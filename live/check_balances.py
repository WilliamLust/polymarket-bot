from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

pusd_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
proxy_address = "0xD47142b12ff69fa02f94f9b0f867E1a40027637F"
deposit_address = "0xf277e98adFE6DD4670c2Bb871941DF628A8E0932"

erc20_abi = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]
contract = w3.eth.contract(address=Web3.to_checksum_address(pusd_address), abi=erc20_abi)

proxy_bal = contract.functions.balanceOf(Web3.to_checksum_address(proxy_address)).call()
deposit_bal = contract.functions.balanceOf(Web3.to_checksum_address(deposit_address)).call()

print(f"Proxy wallet on-chain USDC.e: ${proxy_bal / 1e6:.2f}")
print(f"Deposit wallet on-chain USDC.e: ${deposit_bal / 1e6:.2f}")
