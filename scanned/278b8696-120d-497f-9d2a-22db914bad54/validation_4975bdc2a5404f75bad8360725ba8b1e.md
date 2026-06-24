### Title
No Recovery for Excess ETH/ERC-20 Sent Directly to ckETH Minter's Ethereum Address - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary
The ckETH minter canister controls an Ethereum address via threshold ECDSA. Any ETH or ERC-20 tokens (including supported ckERC20 tokens such as ckWSTETH, ckUSDC, ckUSDT, etc.) sent directly to this address — bypassing the helper smart contract — are permanently unrecoverable. The minter's internal balance tracking only accounts for deposits that arrive via the helper contract's `ReceivedEthOrErc20` log events. No recovery endpoint exists in the minter's public interface to reclaim the surplus.

### Finding Description
The ckETH minter canister exposes its Ethereum address publicly via the `minter_address()` query endpoint. The DID file explicitly warns:

> "IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter knows to which IC principal the funds should be deposited."

Despite this warning, the minter has no on-chain or canister-level mechanism to prevent direct transfers to its Ethereum address, nor any function to recover them.

The `EthBalance` struct in `rs/ethereum/cketh/minter/src/state.rs` explicitly acknowledges the discrepancy:

```rust
/// Amount of ETH controlled by the minter's address via tECDSA.
/// Note that invalid deposits are not accounted for and so this value
/// might be less than what is displayed by Etherscan
/// or retrieved by the JSON-RPC call `eth_getBalance`.
/// Also, some transactions may have gone directly to the minter's address
/// without going via the helper smart contract.
eth_balance: Wei,
```

The `MinterInfo` type in the DID file also acknowledges the divergence for both ETH and ERC-20:

```
// Amount of ETH in Wei controlled by the minter.
// This might be less that the actual amount available on the `minter_address()`.
eth_balance : opt nat;

// Amount of ETH in Wei controlled by the minter.
// This might be less that the actual amount available on the `minter_address()`.
erc20_balances : opt vec record { erc20_contract_address: text; balance: nat};
```

The minter's balance tracking (`eth_balance` and `erc20_balances`) is updated **only** via `update_balance_upon_deposit`, which is called exclusively when a `ReceivedEthOrErc20` log event is scraped from the helper contract. Direct transfers to the minter's Ethereum address produce no such log event and are therefore never recorded.

The complete minter service interface in `cketh_minter.did` exposes only two withdrawal endpoints — `withdraw_eth` and `withdraw_erc20` — both of which require a prior ckETH/ckERC20 burn on the IC ledger. Since no ckETH or ckERC20 is ever minted for directly-transferred funds, these endpoints cannot be used to recover them. There is no `recover_excess_eth`, `recover_excess_erc20`, or equivalent administrative endpoint.

### Impact Explanation
Any ETH or ERC-20 token (USDC, USDT, wstETH, WBTC, LINK, PEPE, SHIB, UNI, EURC, XAUt — all supported ckERC20 tokens) sent directly to the minter's Ethereum address is permanently frozen. The minter's internal accounting diverges from the actual on-chain balance, and there is no path to recover those funds without a canister upgrade that adds a recovery function. This is a **ledger conservation bug**: real on-chain assets are permanently lost with no recourse.

### Likelihood Explanation
The minter's Ethereum address is publicly queryable by any IC user via `minter_address()`. Users may confuse the minter's address with the helper contract address (both are prominently displayed in the minter dashboard and documentation). The documentation warning is advisory only and does not prevent the transfer. Given the number of supported ERC-20 tokens and the public visibility of the minter's address, accidental direct transfers are a realistic scenario.

### Recommendation
Add a privileged (NNS-governance-gated) recovery endpoint to the ckETH minter canister that:
1. Computes the excess ETH balance as `eth_getBalance(minter_address) - eth_balance.eth_balance()`.
2. Computes the excess ERC-20 balance for each supported token as `erc20.balanceOf(minter_address) - erc20_balances.balance_of(contract)`.
3. Issues a signed Ethereum transaction (via threshold ECDSA) to transfer the excess to a designated recovery address, or alternatively mints the corresponding ck-tokens to a designated IC account.

This mirrors the recommendation in the original wstETH report: add a function to recover excess tokens and maintain the integrity of the wrapped-shares accounting.

### Proof of Concept

**Step 1 — Obtain the minter's Ethereum address (permissionless IC query):**
```
dfx canister --network ic call sv3dd-oaaaa-aaaar-qacoa-cai minter_address
```
Returns e.g. `"0x1789F79e95324A47c5Fd6693071188e82E9a3558"`.

**Step 2 — Send ETH or a supported ERC-20 token directly to that address on Ethereum:**
```
# ETH: plain transfer of 0.1 ETH to 0x1789F79e95324A47c5Fd6693071188e82E9a3558
# ERC-20 (e.g. USDC): call transfer(0x1789F79e95324A47c5Fd6693071188e82E9a3558, 1_000_000)
```
No `ReceivedEthOrErc20` event is emitted; the minter never scrapes this transfer.

**Step 3 — Observe the discrepancy:**
```
dfx canister --network ic call sv3dd-oaaaa-aaaar-qacoa-cai get_minter_info
```
`eth_balance` (or the relevant `erc20_balances` entry) remains unchanged, while `eth_getBalance` / `balanceOf` on Ethereum shows the increased balance.

**Step 4 — Confirm no recovery path:**
- `withdraw_eth` requires burning ckETH; no ckETH was minted → `InsufficientFunds`.
- `withdraw_erc20` requires burning ckERC20; no ckERC20 was minted → `InsufficientFunds`.
- No other endpoint in the minter's interface can move the excess funds.

The transferred ETH/ERC-20 is permanently frozen in the minter's Ethereum address.

---

**Key file references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-338)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L648-655)
```rust
pub struct EthBalance {
    /// Amount of ETH controlled by the minter's address via tECDSA.
    /// Note that invalid deposits are not accounted for and so this value
    /// might be less than what is displayed by Etherscan
    /// or retrieved by the JSON-RPC call `eth_getBalance`.
    /// Also, some transactions may have gone directly to the minter's address
    /// without going via the helper smart contract.
    eth_balance: Wei,
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L217-226)
```text
    // Amount of ETH in Wei controlled by the minter.
    // This might be less that the actual amount available on the `minter_address()`.
    eth_balance : opt nat;

    // Last gas fee estimate.
    last_gas_fee_estimate: opt GasFeeEstimate;

    // Amount of ETH in Wei controlled by the minter.
    // This might be less that the actual amount available on the `minter_address()`.
    erc20_balances : opt vec record { erc20_contract_address: text; balance: nat};
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-702)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L718-723)
```text
    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });
```
