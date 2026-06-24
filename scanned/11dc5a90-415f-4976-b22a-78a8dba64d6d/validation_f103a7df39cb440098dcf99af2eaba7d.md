### Title
Rebasing ERC-20 Token Rebase Rewards Permanently Stuck in ckETH Minter's Ethereum Address — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `erc20_balances` accounting is driven exclusively by audit events (deposit log values and withdrawal amounts). If a rebasing ERC-20 token such as stETH is added as a supported ckERC20 token, the minter's actual Ethereum-side balance grows with every rebase while the IC-side accounting never reflects this growth. The accumulated rebase rewards are permanently locked in the minter's Ethereum address with no sweep or rescue mechanism.

---

### Finding Description

**Deposit flow — amount recorded from event log, not actual received balance**

`depositErc20` in `DepositHelperWithSubaccount.sol` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits `ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount)` where `amount` is the caller-supplied parameter: [1](#0-0) 

The IC minter scrapes this log and calls `update_balance_upon_deposit`, which adds `event.value` (the `amount` field from the log) to `erc20_balances`: [2](#0-1) 

`erc20_balances` is explicitly documented as "Computed based on audit events" — it is never reconciled against the minter's actual on-chain Ethereum balance: [3](#0-2) 

**Withdrawal flow — sends exactly the user-requested amount**

When a user calls `withdraw_erc20`, the minter constructs an ERC-20 `transfer` call encoding `request.withdrawal_amount` (the ckERC20 burn amount): [4](#0-3) 

After the transaction is finalized, `update_balance_upon_withdrawal` subtracts only that withdrawal amount from `erc20_balances`: [5](#0-4) 

**The gap**

For a rebasing token like stETH, the minter's Ethereum address balance increases daily (Lido oracle rebases). The minter's `erc20_balances` never increases to reflect this. The delta — the accumulated rebase rewards — sits in the minter's Ethereum address indefinitely. There is no admin endpoint, sweep function, or reconciliation timer that could recover these tokens.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion mint/burn accounting divergence.**

- Every rebase cycle, the minter's actual stETH balance on Ethereum exceeds the sum of all outstanding ckstETH (the ckERC20 supply). The excess is permanently unclaimable.
- The `erc20_balances` metric reported by `get_minter_info` diverges from reality, misleading operators and auditors.
- No user or operator can recover the stuck rebase rewards; there is no `sweep`, `rescue`, or balance-reconciliation endpoint in the minter's interface. [6](#0-5) 

---

### Likelihood Explanation

- Any ERC-20 token can be added as a supported ckERC20 token via a standard NNS upgrade proposal targeting the ledger suite orchestrator. No malicious governance is required — a legitimate proposal to support stETH (a top-5 DeFi token by TVL) would trigger this.
- Once a rebasing token is supported, every deposit by any unprivileged user causes rebase rewards to begin accumulating. No special attacker action is needed beyond making a normal deposit.
- Lido's stETH rebase is daily and automatic; the divergence grows continuously from the moment of the first deposit.

---

### Recommendation

1. **Reject rebasing tokens at the minter level**: When adding a new ckERC20 token, validate that the ERC-20 contract does not implement rebasing semantics (e.g., check that `balanceOf` is not dynamic relative to shares). Alternatively, document explicitly that only non-rebasing tokens are supported and enforce this in the `add_ckerc20_token` handler.
2. **Use share-based accounting**: For tokens like stETH, record and transfer shares (via `transferShares`/`sharesOf`) rather than balance amounts, analogous to the Mellow mitigation of "transferring shares."
3. **Prefer wrapped non-rebasing variants**: Only support `wstETH` (the non-rebasing wrapper of stETH) rather than stETH directly, as suggested in the reference report.
4. **Add a balance-reconciliation query**: Expose a mechanism to compare `erc20_balances` against the actual on-chain balance so that any divergence (from rebasing or fee-on-transfer tokens) is observable and actionable.

---

### Proof of Concept

1. An NNS proposal adds stETH (`0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84`) as a supported ckERC20 token.
2. Alice calls `depositErc20(stETH_address, 10 ether, principal, subaccount)` on `CkDeposit`. The helper transfers 10 stETH to the minter's Ethereum address and emits `ReceivedEthOrErc20(..., 10 ether, ...)`.
3. The IC minter scrapes the log, mints 10 ckstETH to Alice, and sets `erc20_balances[stETH] = 10 ether`.
4. Lido's daily oracle rebase runs. The minter's stETH balance becomes `10.03 ether` (3% APR / 365 days ≈ 0.03 ether per day accumulated over a year).
5. Alice calls `withdraw_erc20(10 ether, ckstETH_ledger, eth_address)`. The minter burns 10 ckstETH, sends 10 stETH to Alice, and sets `erc20_balances[stETH] = 0`.
6. The minter's Ethereum address still holds `~0.03 ether` worth of stETH (the accumulated rebase rewards). `erc20_balances[stETH] = 0` but actual balance > 0. These tokens are permanently stuck with no recovery path. [1](#0-0) [2](#0-1) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L511-532)
```text
    function depositErc20(
        address erc20Address,
        uint256 amount,
        bytes32 principal,
        bytes32 subaccount
    ) public {
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
        IERC20 erc20Token = IERC20(erc20Address);
        erc20Token.safeTransferFrom(
            msg.sender,
            minterAddress,
            amount
        );

        emit ReceivedEthOrErc20(
            erc20Address,
            msg.sender,
            amount,
            principal,
            subaccount
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L74-76)
```rust
    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-339)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L377-383)
```rust
        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1175-1183)
```rust
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L218-226)
```text
    // This might be less that the actual amount available on the `minter_address()`.
    eth_balance : opt nat;

    // Last gas fee estimate.
    last_gas_fee_estimate: opt GasFeeEstimate;

    // Amount of ETH in Wei controlled by the minter.
    // This might be less that the actual amount available on the `minter_address()`.
    erc20_balances : opt vec record { erc20_contract_address: text; balance: nat};
```
