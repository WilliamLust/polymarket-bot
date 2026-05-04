"""
AI Probability Arbitrage strategy.

Uses local LLM (Qwen 3.6:27b via Ollama) to estimate true probability
from news/sentiment, then trades when market price diverges > threshold.

This is the GPU-utilizing strategy — the RTX 3090 matters here.
"""

import json
import requests
from dataclasses import dataclass


@dataclass
class ProbabilityEstimate:
    """Output from the AI probability estimation pipeline."""
    probability: float          # Estimated true probability (0-1)
    confidence: float           # Model confidence (0-1)
    sources_used: list[str]     # News sources analyzed
    reasoning: str              # Brief reasoning chain
    market_id: str              # Market this estimate is for


class AIProbArb:
    """
    AI Probability Arbitrage engine.
    
    Workflow:
    1. Monitor news sources (Reuters, AP, Bloomberg APIs)
    2. Cross-reference with X sentiment (xurl)
    3. Run ensemble of models → probability estimate
    4. Compare with market price
    5. Signal trade when divergence > threshold
    """

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = "qwen3.6:27b",
        divergence_threshold: float = 0.15,
        kelly_fraction: float = 0.25,
        max_position: float = 0.05,
    ):
        self.ollama_url = ollama_url
        self.model = model
        self.divergence_threshold = divergence_threshold
        self.kelly_fraction = kelly_fraction
        self.max_position = max_position

    def estimate_probability(
        self,
        market_question: str,
        context: str,
        current_market_price: float,
    ) -> ProbabilityEstimate:
        """
        Use local LLM to estimate the true probability of a market outcome.
        
        Args:
            market_question: The prediction market question
            context: Aggregated news/sentiment text
            current_market_price: Current market-implied probability
            
        Returns:
            ProbabilityEstimate with model's assessment
        """
        prompt = f"""You are a prediction market analyst. Estimate the true probability of this event occurring.

MARKET QUESTION: {market_question}

CONTEXT (news and sentiment):
{context}

CURRENT MARKET PRICE: {current_market_price:.2f} (this is the crowd's estimate)

Analyze the evidence and estimate the TRUE probability. Do NOT anchor on the market price.
Consider: source credibility, recency, sample size, base rates, known biases.

Respond in this exact JSON format:
{{"probability": 0.XX, "confidence": 0.XX, "reasoning": "brief explanation"}}"""

        try:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 500},
            }
            # Qwen 3.x puts content in "thinking" field unless think:false is set
            # Try think:false first; if model doesn't support it, fall back
            try:
                payload["think"] = False
                resp = requests.post(
                    f"{self.ollama_url}/api/generate",
                    json=payload,
                    timeout=90,
                )
            except Exception:
                del payload["think"]
                resp = requests.post(
                    f"{self.ollama_url}/api/generate",
                    json=payload,
                    timeout=90,
                )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("response", "")
            # If response is empty (Qwen 3.x thinking mode), extract from thinking
            if not raw.strip():
                thinking = data.get("thinking", "")
                # The thinking field contains the reasoning; extract JSON from it
                raw = thinking
            
            # Extract JSON from response (may have surrounding text)
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end])
                return ProbabilityEstimate(
                    probability=float(parsed.get("probability", current_market_price)),
                    confidence=float(parsed.get("confidence", 0.5)),
                    sources_used=["local_llm"],
                    reasoning=parsed.get("reasoning", ""),
                    market_id="",
                )
        except Exception as e:
            print(f"LLM estimation failed: {e}")
        
        # Fallback: return market price (no edge detected)
        return ProbabilityEstimate(
            probability=current_market_price,
            confidence=0.0,
            sources_used=[],
            reasoning="Estimation failed, defaulting to market price",
            market_id="",
        )

    def evaluate_signal(
        self,
        estimate: ProbabilityEstimate,
        market_price: float,
    ) -> dict:
        """
        Determine if a trade signal exists based on probability divergence.
        
        Returns dict with signal details or None if no edge.
        """
        divergence = estimate.probability - market_price
        abs_divergence = abs(divergence)
        
        if abs_divergence < self.divergence_threshold:
            return {"signal": "none", "divergence": divergence, "reason": "Below threshold"}
        
        direction = "buy_yes" if divergence > 0 else "buy_no"
        
        # Position sizing via Kelly
        from strategies.stacking_ensemble import kelly_criterion
        
        if direction == "buy_yes":
            size = kelly_criterion(estimate.probability, market_price, self.kelly_fraction)
        else:
            size = kelly_criterion(1 - estimate.probability, 1 - market_price, self.kelly_fraction)
        
        return {
            "signal": direction,
            "divergence": divergence,
            "estimated_prob": estimate.probability,
            "market_price": market_price,
            "position_size": size,
            "confidence": estimate.confidence,
            "reasoning": estimate.reasoning,
        }


def check_ollama_available(ollama_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama is running and the model is available."""
    try:
        resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return any("qwen" in m.lower() for m in models)
    except Exception:
        pass
    return False
