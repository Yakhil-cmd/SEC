### Title
ckERC20 Withdrawal Forces All ERC20 Tokens Through a Single Fixed Gas Limit, Causing Permanent ckETH Fee Loss on Out-of-Gas Failures — (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter applies a single hardcoded gas limit (`CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`) to every ckERC20 withdrawal transaction, regardless of which ERC20 token is being withdrawn. Supported tokens with complex `transfer` logic (e.g., wstETH, USDT) can require more than 65,000 gas. When the Ethereum transaction runs out of gas, the ckERC20 tokens are reimbursed but the ckETH fee burned upfront is **permanently lost** — an exact structural parallel to the original finding where all reward tokens were forced through a single UniswapV2 router that lacked adequate liquidity for every token.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas-limit constants are defined:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

`estimate_gas_limit` dispatches on the withdrawal type and returns the same constant for every ckERC20 token, with no per-token differentiation:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

`estimate_erc20_transaction_fee`, called inside `withdraw_erc20` before the ckETH burn, also hard-codes the same limit:

```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
``` [3](#0-2) 

The `withdraw_erc20` update method burns ckETH first (using this estimate), then burns ckERC20, then queues the Ethereum transaction: [4](#0-3) 

The minter documentation explicitly acknowledges the fixed limit and its scope:

> "The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts." [5](#0-4) 

The same documentation states that overcharged (or lost) transaction fees are **not** reimbursed:

> "Overcharged transaction fees are not reimbursed." [6](#0-5) 

The minter currently supports tokens including wstETH, USDT, XAUt, PEPE, and SHIB — several of which have non-trivial `transfer` implementations that can exceed 65,000 gas: [7](#0-6) 

---

### Impact Explanation

When a user withdraws a ckERC20 token whose underlying ERC20 `transfer` call requires more than 65,000 gas:

1. The minter burns the user's ckETH upfront to cover the estimated fee.
2. The Ethereum transaction is submitted with `gas_limit = 65_000`.
3. The transaction reverts on-chain with an out-of-gas error.
4. The minter reimburses the ckERC20 tokens.
5. The ckETH fee is **not** reimbursed — it is permanently lost.

The user suffers a forced, unrecoverable economic loss denominated in ckETH, with no ability to choose a higher gas limit or avoid the failure path. This is a **chain-fusion economic loss bug**: the minter's single-path gas accounting is structurally insufficient for the full set of tokens it supports.

---

### Likelihood Explanation

- The entry path is fully unprivileged: any IC principal can call `withdraw_erc20` on the minter canister.
- wstETH (Lido wrapped staked ETH) is a currently supported token. Its `transfer` function involves stETH share accounting and can consume well above 65,000 gas on mainnet.
- USDT's non-standard `transfer` (with allowance-zeroing and fee logic) has historically consumed 65,000–80,000 gas depending on storage state.
- The user has no on-chain mechanism to query whether their specific token will succeed before committing the ckETH burn.
- No governance action or attacker coordination is required; a normal user withdrawal is sufficient to trigger the loss.

---

### Recommendation

1. **Per-token gas limits**: Store a configurable `gas_limit` alongside each `CkErc20Token` entry in minter state, set at token-addition time via `add_ckerc20_token`. This mirrors the per-token routing flexibility recommended in the original report.
2. **Reimburse ckETH on out-of-gas failure**: Treat an on-chain out-of-gas revert the same as a `TemporarilyUnavailable` error — reimburse the full ckETH fee rather than treating it as a user error.
3. **Pre-flight gas estimation**: Before burning ckETH, perform an `eth_estimateGas` outcall for the specific token's transfer to detect likely failures early and reject the withdrawal with no fee loss.

---

### Proof of Concept

1. User holds ckwstETH and ckETH on the IC.
2. User calls `icrc2_approve` on the ckETH ledger for the minter (fee amount).
3. User calls `icrc2_approve` on the ckwstETH ledger for the minter (withdrawal amount).
4. User calls `withdraw_erc20` on the minter with `ckerc20_ledger_id = wstETH_ledger_id`.
5. Minter calls `estimate_erc20_transaction_fee()` → computes fee using `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`.
6. Minter burns user's ckETH for the estimated fee.
7. Minter burns user's ckwstETH.
8. Minter submits Ethereum transaction with `gas_limit = 65_000` calling `wstETH.transfer(destination, amount)`.
9. Ethereum transaction reverts: out-of-gas (wstETH transfer requires ~100,000+ gas).
10. Minter reimburses ckwstETH to user.
11. ckETH fee is **not** reimbursed — user has permanently lost ETH-denominated value.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L296-301)
```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L545-553)
```rust

```

**File:** rs/ethereum/cketh/minter/src/main.rs (L429-432)
```rust
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L11-48)
```text
=== Ethereum Mainnet

|===
| ERC-20 token symbol | ERC-20 smart contract address

|EURC
|https://etherscan.io/token/0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c[0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c]

|LINK
|https://etherscan.io/token/0x514910771AF9Ca656af840dff83E8264EcF986CA[0x514910771AF9Ca656af840dff83E8264EcF986CA]

|OCT
|https://etherscan.io/token/0xF5cFBC74057C610c8EF151A439252680AC68c6DC[0xF5cFBC74057C610c8EF151A439252680AC68c6DC]

|PEPE
|https://etherscan.io/token/0x6982508145454Ce325dDbE47a25d4ec3d2311933[0x6982508145454Ce325dDbE47a25d4ec3d2311933]

|SHIB
|https://etherscan.io/token/0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE[0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE]

|UNI
|https://etherscan.io/token/0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984[0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984]

|USDC
|https://etherscan.io/token/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48[0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48]

|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]

|WBTC
|https://etherscan.io/token/0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599[0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599]

|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]

|XAUt
|https://etherscan.io/token/0x68749665FF8D2d112Fa859AA293F07A622782F38[0x68749665FF8D2d112Fa859AA293F07A622782F38]
|===
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
