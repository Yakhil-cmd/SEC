### Title
ckERC20 Minter Tracks ERC-20 Balances Purely via Deposit/Withdrawal Events With No On-Chain Reconciliation, Permanently Locking Rebasing Token Rewards — (File: `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `Erc20Balances` struct maintains ERC-20 token balances by summing deposit events and subtracting withdrawal events, with no mechanism to reconcile against the actual on-chain balance. For rebasing ERC-20 tokens — where a holder's balance increases over time without any `Transfer` event — the minter's tracked balance permanently diverges from the actual on-chain balance, causing rebasing rewards to accumulate in the minter's Ethereum address with no mechanism to distribute them to ckERC20 holders.

---

### Finding Description

`Erc20Balances` in `rs/ethereum/cketh/minter/src/state.rs` is a pure event-driven accounting struct:

```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct Erc20Balances {
    balance_by_erc20_contract: BTreeMap<Address, Erc20Value>,
}
``` [1](#0-0) 

It is updated in exactly two places:

1. **On deposit** — `update_balance_upon_deposit` adds `event.value` (the amount from the Ethereum log):

```rust
ReceivedEvent::Erc20(event) => self
    .erc20_balances
    .erc20_add(event.erc20_contract_address, event.value),
``` [2](#0-1) 

2. **On withdrawal finalization** — `update_balance_upon_withdrawal` subtracts the transferred amount:

```rust
self.erc20_balances.erc20_sub(*tx.destination(), value);
``` [3](#0-2) 

There is no third path: no periodic `eth_call` to `balanceOf(minterAddress)`, no reconciliation loop, and no concept of shares vs. balance. The minter mints ckERC20 1:1 with `event.value()`:

```rust
amount: event.value(),
``` [4](#0-3) 

For a rebasing ERC-20 token, the holder's balance increases over time without emitting a `Transfer` event. The minter's Ethereum address accumulates the rebased tokens silently. The minter's tracked `erc20_balances` never reflects this growth, and no ckERC20 is ever minted for the rebased amount. The excess tokens are permanently inaccessible.

The minter is explicitly designed to support any ERC-20 token added via NNS governance. The NNS has already approved **wstETH** (`0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0`) — the non-rebasing wrapper of stETH — as a supported ckERC20 token, demonstrating active engagement with the stETH ecosystem: [5](#0-4) 

The supported token list in the documentation also shows wstETH: [6](#0-5) 

The minter has no technical guard preventing a rebasing token (e.g., stETH itself) from being added via the same governance path.

---

### Impact Explanation

If a rebasing ERC-20 token is added as a supported ckERC20 token:

- The minter mints ckERC20 equal to the deposited amount at deposit time.
- The actual ERC-20 balance held by the minter's Ethereum address grows continuously due to rebasing.
- The ckERC20 total supply remains fixed at the sum of all deposits minus withdrawals.
- The rebasing delta — potentially large over time — is permanently locked in the minter's Ethereum address with no mechanism to claim or distribute it.
- The invariant `ckERC20 total supply == minter's actual ERC-20 balance` is broken, constituting a **ledger conservation bug** in the chain-fusion bridge.

The `erc20_balances` field exposed via `get_minter_info` would also report a stale, incorrect value, misleading operators and users about the true collateralization ratio. [7](#0-6) 

---

### Likelihood Explanation

**Medium-Low.** The trigger requires an NNS governance proposal to add a rebasing ERC-20 token. This is the standard, legitimate mechanism for adding new ckERC20 tokens — not a malicious action. The NNS has already approved wstETH (the non-rebasing stETH wrapper), showing active interest in the stETH ecosystem. A future proposal to add stETH directly, or any other rebasing token (e.g., aTokens from Aave, which also rebase), would silently activate this bug. The minter contains no technical safeguard, no documentation warning, and no validation that a token is non-rebasing before accepting it.

---

### Recommendation

1. **Document the limitation**: Explicitly state in the ckERC20 documentation and in the `add_ckerc20_token` endpoint that rebasing ERC-20 tokens are not supported.
2. **Add a balance reconciliation mechanism**: Implement a periodic `eth_call` to `balanceOf(minterAddress)` for each supported ERC-20 token and compare it to the tracked `erc20_balances`. Alert or halt if a discrepancy is detected.
3. **Shares-based accounting**: If rebasing tokens are to be supported in the future, implement a shares-based accounting system (analogous to how wstETH wraps stETH) so that the minter records shares at deposit time and converts to balance at withdrawal time.

---

### Proof of Concept

1. NNS governance approves stETH (`0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84`) as a supported ckERC20 token via `add_ckerc20_token`.
2. User calls `depositErc20(stETH_address, 100e18, principal, subaccount)` on the helper contract. The helper calls `transferFrom(user, minterAddress, 100e18)` and emits `ReceivedEthOrErc20`.
3. The minter scrapes the log, observes `event.value = 100e18`, calls `erc20_add(stETH_address, 100e18)`, and mints 100 ckstETH to the user.
4. Over the next year, stETH rebases at ~4% APY. The minter's actual stETH balance grows to ~104e18.
5. The minter's tracked `erc20_balances` still shows `100e18`. No new deposit event is emitted by stETH's rebasing mechanism.
6. The user can only withdraw 100 ckstETH (burning 100 ckstETH to receive 100 stETH). The 4 stETH rebasing reward is permanently locked in the minter's Ethereum address.
7. Over time, as more users deposit and rebasing continues, the locked surplus grows unboundedly, with no on-chain or off-chain mechanism in the minter to recover it. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L335-338)
```rust
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L382-383)
```rust
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L729-732)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct Erc20Balances {
    balance_by_erc20_contract: BTreeMap<Address, Erc20Value>,
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L742-756)
```rust
    pub fn erc20_add(&mut self, erc20_contract: Address, deposit: Erc20Value) {
        match self.balance_by_erc20_contract.get(&erc20_contract) {
            Some(previous_value) => {
                let new_value = previous_value.checked_add(deposit).unwrap_or_else(|| {
                    panic!("BUG: overflow when adding {deposit} to {previous_value}")
                });
                self.balance_by_erc20_contract
                    .insert(erc20_contract, new_value);
            }
            None => {
                self.balance_by_erc20_contract
                    .insert(erc20_contract, deposit);
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L758-770)
```rust
    pub fn erc20_sub(&mut self, erc20_contract: Address, withdrawal_amount: Erc20Value) {
        let previous_value = self
            .balance_by_erc20_contract
            .get(&erc20_contract)
            .expect("BUG: Cannot subtract from a missing ERC-20 balance");
        let new_value = previous_value
            .checked_sub(withdrawal_amount)
            .unwrap_or_else(|| {
                panic!("BUG: underflow when subtracting {withdrawal_amount} from {previous_value}")
            });
        self.balance_by_erc20_contract
            .insert(erc20_contract, new_value);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L80-81)
```rust
                amount: event.value(),
            })
```

**File:** rs/ethereum/cketh/mainnet/orchestrator_upgrade_2024_07_26.md (L1-16)
```markdown
# Proposal to upgrade the ledger suite orchestrator canister to add ckWSTETH

Git hash: `de29a1a55b589428d173b31cdb8cec0923245657`

New compressed Wasm hash: `81f426bcc52140fdcf045d02d00b04bfb4965445b8aed7090d174fcdebf8beea`

Target canister: `vxkom-oyaaa-aaaar-qafda-cai`

Previous ledger suite orchestrator proposal: https://dashboard.internetcomputer.org/proposal/131374

---

## Motivation

This proposal upgrades the ckERC20 ledger suite orchestrator to add support for [wstETH](https://etherscan.io/token/0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0#tokenInfo). Once executed, the twin token ckWSTETH will be available on ICP, refer to the [documentation](https://github.com/dfinity/ic/blob/master/rs/ethereum/cketh/docs/ckerc20.adoc) on how to proceed with deposits and withdrawals.

```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L43-44)
```text
|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L207-218)
```rust
        let (erc20_balances, supported_ckerc20_tokens) = if s.is_ckerc20_feature_active() {
            let (balances, tokens) = s
                .supported_ck_erc20_tokens()
                .map(|token| {
                    (
                        Erc20Balance {
                            erc20_contract_address: token.erc20_contract_address.to_string(),
                            balance: s
                                .erc20_balances
                                .balance_of(&token.erc20_contract_address)
                                .into(),
                        },
```
