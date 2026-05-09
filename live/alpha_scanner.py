#!/usr/bin/env python3
"""X.com Polymarket Alpha Scanner

Searches X for Polymarket trading strategy posts, extracts structured data,
and appends to a dated JSON log for later review by the trading bot operator.

Designed to run as a Hermes cron job every 6 hours.
Output goes to ~/polymarket-bot/live/shadow_data/alpha_scans/
"""

import json
import os
import sys
from datetime import datetime, timezone

DATA_DIR = os.path.expanduser("~/polymarket-bot/live/shadow_data/alpha_scans")
os.makedirs(DATA_DIR, exist_ok=True)

# Search queries — each targets a different angle
QUERIES = [
    "polymarket trading bot strategy",
    "polymarket alpha signal backtest",
    "polymarket arbitrage NO YES edge",
    "prediction market strategy crypto",
]

# Filter keywords — skip posts containing these
SPAM_KEYWORDS = [
    "giveaway", "airdrop", "follow me", "retweet to win", 
    "nft", "minting", "pump.fun", "$SOL",
    "hire me", "freelance", "dm me",
]

# High-signal author handles (add more as discovered)
ALERT_AUTHORS = [
    "PolyDekos", "ventry089", "0xEmoni", "0xRicker",
    "Damir_Akaza", "L1vsun", "AlterEgo_eth", "dunik_7",
]


def run():
    import subprocess
    
    all_results = []
    scan_time = datetime.now(timezone.utc).isoformat()
    
    for query in QUERIES:
        try:
            result = subprocess.run(
                ["xurl", "search", query, "-n", "20"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                print(f"Search failed for '{query}': {result.stderr[:200]}")
                continue
            
            data = json.loads(result.stdout)
            posts = data.get("data", [])
            
            for post in posts:
                text = post.get("text", "")
                author_id = post.get("author_id", "")
                post_id = post.get("id", "")
                metrics = post.get("public_metrics", {})
                
                # Skip spam
                text_lower = text.lower()
                if any(kw in text_lower for kw in SPAM_KEYWORDS):
                    continue
                
                # Skip very short posts (likely noise)
                if len(text) < 50:
                    continue
                
                # Compute engagement score
                likes = metrics.get("like_count", 0)
                retweets = metrics.get("retweet_count", 0)
                replies = metrics.get("reply_count", 0)
                bookmarks = metrics.get("bookmark_count", 0)
                impressions = metrics.get("impression_count", 0)
                
                engagement = likes + retweets * 3 + replies * 2 + bookmarks * 5
                
                # Get author username from includes if available
                author_name = ""
                includes = data.get("includes", {})
                users = includes.get("users", [])
                for u in users:
                    if u.get("id") == author_id:
                        author_name = u.get("username", "")
                        break
                
                # Check if high-signal author
                is_alert = author_name in ALERT_AUTHORS
                
                entry = {
                    "post_id": post_id,
                    "author": author_name,
                    "author_id": author_id,
                    "text": text[:500],  # Truncate long posts
                    "likes": likes,
                    "retweets": retweets,
                    "replies": replies,
                    "bookmarks": bookmarks,
                    "impressions": impressions,
                    "engagement_score": engagement,
                    "query": query,
                    "is_alert_author": is_alert,
                    "scanned_at": scan_time,
                }
                all_results.append(entry)
                
        except json.JSONDecodeError:
            print(f"Bad JSON from search '{query}'")
        except subprocess.TimeoutExpired:
            print(f"Timeout on search '{query}'")
        except Exception as e:
            print(f"Error on search '{query}': {e}")
    
    # Deduplicate by post_id
    seen = set()
    unique = []
    for r in all_results:
        if r["post_id"] not in seen:
            seen.add(r["post_id"])
            unique.append(r)
    
    # Sort by engagement score descending
    unique.sort(key=lambda x: x["engagement_score"], reverse=True)
    
    # Write dated output file
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outfile = os.path.join(DATA_DIR, f"scan_{date_str}.json")
    
    # Append to existing file or create new
    if os.path.exists(outfile):
        with open(outfile, "r") as f:
            existing = json.load(f)
        existing_ids = {r["post_id"] for r in existing}
        new_entries = [r for r in unique if r["post_id"] not in existing_ids]
        existing.extend(new_entries)
        # Re-sort
        existing.sort(key=lambda x: x["engagement_score"], reverse=True)
        with open(outfile, "w") as f:
            json.dump(existing, f, indent=2)
        total_new = len(new_entries)
        total_file = len(existing)
    else:
        with open(outfile, "w") as f:
            json.dump(unique, f, indent=2)
        total_new = len(unique)
        total_file = len(unique)
    
    # Print summary for cron delivery
    alert_count = sum(1 for r in unique if r.get("is_alert_author"))
    top3 = unique[:3] if unique else []
    
    print(f"Alpha scan complete: {total_new} new posts, {total_file} total in {outfile}")
    if alert_count:
        print(f"  Alert authors: {alert_count}")
    if top3:
        print(f"  Top post: @{top3[0]['author']} (engagement={top3[0]['engagement_score']}) — {top3[0]['text'][:80]}...")
    
    # Prune files older than 30 days
    now = datetime.now(timezone.utc)
    for fname in os.listdir(DATA_DIR):
        if fname.startswith("scan_") and fname.endswith(".json"):
            fpath = os.path.join(DATA_DIR, fname)
            if os.path.getmtime(fpath) < (now.timestamp() - 30 * 86400):
                os.remove(fpath)
                print(f"  Pruned old file: {fname}")


if __name__ == "__main__":
    run()
