/**
 * Bridge Transfer — Move native USDC from deposit wallet to Polymarket Bridge
 *
 * The deposit wallet holds native USDC which is PAUSED on the CollateralOnramp.
 * This script transfers USDC to the Bridge API's EVM deposit address, which
 * auto-converts it to pUSD and credits the CLOB balance.
 *
 * Usage:
 *   node live/bridge_transfer.js              # transfer full USDC balance
 *   node live/bridge_transfer.js --amount 49  # transfer $49 (leave dust)
 *   node live/bridge_transfer.js --check      # check balance only, no transfer
 */

const { RelayClient } = require("@polymarket/builder-relayer-client");
const { BuilderConfig } = require("@polymarket/builder-signing-sdk");
const { createWalletClient, createPublicClient, http, encodeFunctionData } = require("viem");
const { privateKeyToAccount } = require("viem/accounts");
const { polygon } = require("viem/chains");
const axios = require("axios");
const path = require("path");

// ── Constants ──────────────────────────────────────────────
const RELAYER_URL = "https://relayer-v2.polymarket.com";
const CHAIN_ID = 137;
const DEPOSIT_WALLET = "0xf277e98adFE6DD4670c2Bb871941DF628A8E0932";
const BRIDGE_EVM = "0x21f6F035C913C9fE0525BF44B3555453867DCe2B";
const USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359";

const ERC20_ABI = [
  {
    name: "balanceOf",
    type: "function",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ type: "uint256" }],
    stateMutability: "view",
  },
  {
    name: "transfer",
    type: "function",
    inputs: [
      { name: "to", type: "address" },
      { name: "amount", type: "uint256" },
    ],
    outputs: [{ type: "bool" }],
    stateMutability: "nonpayable",
  },
];

// ── Load env ───────────────────────────────────────────────
require("dotenv").config({ path: path.join(__dirname, "..", ".env") });

const PK = process.env.POLYMARKET_PRIVATE_KEY;
const BUILDER_KEY = process.env.BUILDER_API_KEY;
const BUILDER_SECRET = process.env.BUILDER_SECRET;
const BUILDER_PASSPHRASE = process.env.BUILDER_PASS_PHRASE;

if (!PK || PK === "PASTE_YOUR_PRIVATE_KEY_HERE") {
  console.error("POLYMARKET_PRIVATE_KEY not set in .env");
  process.exit(1);
}
if (!BUILDER_KEY || !BUILDER_SECRET || !BUILDER_PASSPHRASE) {
  console.error("BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE required in .env");
  process.exit(1);
}

async function main() {
  const args = process.argv.slice(2);
  const checkOnly = args.includes("--check");
  const amountArg = args.find((a) => a.startsWith("--amount="));
  const overrideAmount = amountArg ? parseFloat(amountArg.split("=")[1]) : null;

  // 1. Setup wallet + clients
  const account = privateKeyToAccount(PK.startsWith("0x") ? PK : `0x${PK}`);
  const walletClient = createWalletClient({ account, chain: polygon, transport: http() });
  const publicClient = createPublicClient({ chain: polygon, transport: http() });
  console.log(`EOA: ${account.address}`);

  const builderCreds = {
    key: BUILDER_KEY,
    secret: BUILDER_SECRET,
    passphrase: BUILDER_PASSPHRASE,
  };
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });
  const relayClient = new RelayClient(RELAYER_URL, CHAIN_ID, walletClient, builderConfig);

  // 2. Verify deposit wallet is deployed
  try {
    const statusResp = await axios.get(`${RELAYER_URL}/deposit-wallet/deployed`, {
      params: { address: DEPOSIT_WALLET, type: "WALLET" },
      timeout: 10000,
    });
    console.log(`Deposit wallet deployed: ${statusResp.data?.deployed}`);
    if (!statusResp.data?.deployed) {
      console.error("Deposit wallet not deployed! Aborting.");
      process.exit(1);
    }
  } catch (e) {
    console.error(`Deploy check failed: ${e.message}`);
    process.exit(1);
  }

  // 3. Read on-chain USDC balance
  let usdcBalance;
  try {
    usdcBalance = await publicClient.readContract({
      address: USDC_NATIVE,
      abi: ERC20_ABI,
      functionName: "balanceOf",
      args: [DEPOSIT_WALLET],
    });
    const usdcHuman = Number(usdcBalance) / 1e6;
    console.log(`USDC balance: $${usdcHuman.toFixed(6)}`);

    if (usdcHuman < 2) {
      console.error("Balance below $2 minimum bridge deposit. Nothing to transfer.");
      process.exit(1);
    }
  } catch (e) {
    console.error(`Balance read failed: ${e.message}`);
    process.exit(1);
  }

  if (checkOnly) {
    console.log("Check-only mode. Exiting.");
    return;
  }

  // 4. Determine transfer amount
  let transferAmount;
  if (overrideAmount !== null) {
    transferAmount = BigInt(Math.floor(overrideAmount * 1e6));
    if (transferAmount > usdcBalance) {
      console.error(`Requested $${overrideAmount} exceeds balance. Using full balance instead.`);
      transferAmount = usdcBalance;
    }
  } else {
    // Transfer full balance minus $0.01 dust to avoid rounding issues
    transferAmount = usdcBalance - BigInt(10000);
    if (transferAmount <= 0) transferAmount = usdcBalance;
  }

  const transferHuman = Number(transferAmount) / 1e6;
  console.log(`\nWill transfer $${transferHuman.toFixed(2)} USDC to bridge address:`);
  console.log(`  From: ${DEPOSIT_WALLET}`);
  console.log(`  To:   ${BRIDGE_EVM}`);

  // 5. Verify bridge deposit address
  try {
    const bridgeResp = await axios.post(
      "https://bridge.polymarket.com/deposit",
      { address: DEPOSIT_WALLET },
      { headers: { "Content-Type": "application/json" }, timeout: 10000 }
    );
    const bridgeEvm = bridgeResp.data?.address?.evm;
    console.log(`  Bridge confirms EVM deposit addr: ${bridgeEvm}`);
    if (bridgeEvm && bridgeEvm.toLowerCase() !== BRIDGE_EVM.toLowerCase()) {
      console.error("MISMATCH! Bridge returned different address. Aborting.");
      console.error(`  Expected: ${BRIDGE_EVM}`);
      console.error(`  Got:      ${bridgeEvm}`);
      process.exit(1);
    }
  } catch (e) {
    console.error(`Bridge address check failed: ${e.message}`);
    console.error("Continuing anyway — the address was verified in a previous session.");
  }

  // 6. Build transfer calldata
  const transferData = encodeFunctionData({
    abi: [
      {
        name: "transfer",
        type: "function",
        inputs: [
          { name: "to", type: "address" },
          { name: "amount", type: "uint256" },
        ],
        outputs: [{ type: "bool" }],
      },
    ],
    args: [BRIDGE_EVM, transferAmount],
  });

  const calls = [
    {
      target: USDC_NATIVE,
      value: "0",
      data: transferData,
    },
  ];

  // 7. Submit relayer batch
  const deadline = Math.floor(Date.now() / 1000 + 3600).toString();
  console.log(`\nSubmitting relayer batch (deadline: 1hr)...`);

  try {
    const response = await relayClient.executeDepositWalletBatch(calls, DEPOSIT_WALLET, deadline);
    console.log(`Batch response:`, JSON.stringify(response, null, 2));

    // If there's a tx hash, wait for it
    if (response.hash || response.transactionHash) {
      const txHash = response.hash || response.transactionHash;
      console.log(`\nTX submitted: ${txHash}`);
      console.log(`Waiting for confirmation...`);

      const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
      console.log(`TX status: ${receipt.status === "success" ? "SUCCESS" : "FAILED"}`);
      console.log(`Block: ${receipt.blockNumber}`);
    }

    if (response.wait) {
      console.log("Waiting for batch finality...");
      await response.wait();
      console.log("Batch finalized.");
    }
  } catch (e) {
    const errMsg = e.response?.data || e.message || String(e);
    console.error(`\nBatch submission failed: ${errMsg}`);

    if (String(errMsg).includes("batch would revert")) {
      console.error("\nThe underlying contract call would revert. Possible causes:");
      console.error("  - Insufficient USDC balance");
      console.error("  - USDC contract paused/frozen");
      console.error("  - Invalid transfer target");
    }
    process.exit(1);
  }

  // 8. Verify USDC left the deposit wallet
  console.log("\nVerifying transfer...");
  await new Promise((r) => setTimeout(r, 5000)); // wait 5s for chain state
  try {
    const newBalance = await publicClient.readContract({
      address: USDC_NATIVE,
      abi: ERC20_ABI,
      functionName: "balanceOf",
      args: [DEPOSIT_WALLET],
    });
    console.log(`USDC balance after transfer: $${(Number(newBalance) / 1e6).toFixed(6)}`);
  } catch (e) {
    console.log(`Post-transfer balance check failed: ${e.message}`);
  }

  // 9. Check bridge status
  console.log("\nChecking bridge processing status...");
  try {
    const statusResp = await axios.get(
      `https://bridge.polymarket.com/status/${DEPOSIT_WALLET}`,
      { timeout: 10000 }
    );
    console.log(`Bridge status:`, JSON.stringify(statusResp.data, null, 2));
  } catch (e) {
    console.log(`Bridge status check failed: ${e.message}`);
    console.log("Bridge processing may take a few minutes. Check manually:");
    console.log(`  curl https://bridge.polymarket.com/status/${DEPOSIT_WALLET}`);
  }

  // 10. Next steps
  console.log("\n" + "─".repeat(60));
  console.log("NEXT STEPS:");
  console.log("1. Wait 2-5 minutes for bridge to process");
  console.log(`2. Check: curl https://bridge.polymarket.com/status/${DEPOSIT_WALLET}`);
  console.log("3. Sync CLOB balance (run live_trader_node.js in dry-run first)");
  console.log("4. Go live: node live/live_trader_node.js --live --loop --interval=300 --position-size=1");
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});
