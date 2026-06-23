Looking at the vulnerability class — **unbounded cross-chain token release causing resource exhaustion and permanent asset lock** — I need to find an IC analog where an unbounded loop in a chain-fusion minting/release function can exhaust the IC instruction limit, causing tokens to be locked.

Let me examine the ckETH minter's `mint()` function and compare it to the batched withdrawal processing.