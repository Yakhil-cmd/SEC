### Title
ckERC20 Minter Mints Based on Event-Logged Amount Without Reconciling Actual Balance, Breaking Invariant for Rebasing ERC-20 Tokens - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`, `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's chain-fusion deposit flow for ERC-20 tokens trusts the `amount` parameter emitted in the `ReceivedErc20` / `ReceivedEthOrErc20` event log and mints exactly that many ckERC20 tokens. The minter's internal `Erc20Balances` ledger is updated purely from these event values and is never reconciled against the actual on-chain ERC-20 balance held at the minter's Ethereum address. For rebasing ERC-20 tokens (e.g., AMPL), the minter's actual token holdings can silently diverge from the tracked balance after any rebase, causing the ckERC20 supply to become either over- or under-collateralized.

---

### Finding Description

**Deposit helper contracts emit the caller-supplied `amount`, not the actual received amount.**

`DepositHelperWithSubaccount.sol::depositErc20` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits `ReceivedEthOrErc20(erc20Address, msg.sender, amount, ...)` using the same `amount` parameter: [1](#0-0) 

`ERC20DepositHelper.sol::deposit` does the same: [2](#0-1) 

**The minter mints exactly `event.value()` without any balance check.**

In `deposit.rs`, the `mint()` function iterates over `events_to_mint` and calls the ICRC-1 ledger `transfer` with `amount: event.value()`: [3](#0-2) 

**The internal balance tracker is updated from the same event value.**

`update_balance_upon_deposit` in `state.rs` adds `event.value` to `erc20_balances`: [4](#0-3) 

**`Erc20Balances` never queries the actual on-chain balance.**

The `Erc20Balances` struct is a pure in-memory accounting ledger driven entirely by deposit/withdrawal events. It has no mechanism to reconcile with the actual ERC-20 balance at the minter's Ethereum address: [5](#0-4) 

**For a rebasing token**, after a rebase event on Ethereum:
- **Positive rebase** (supply expands): the minter's actual token balance increases, but `erc20_balances` does not. The excess tokens are permanently locked at the minter's Ethereum address with no recovery path.
- **Negative rebase** (supply contracts): the minter's actual token balance decreases below what `erc20_balances` tracks. The total ckERC20 supply now exceeds the backing collateral. Withdrawal transactions sent to Ethereum will fail (insufficient balance), and while the minter reimburses ckERC20 on failure, the system is undercollateralized — not all ckERC20 holders can redeem.

---

### Impact Explanation

If a rebasing ERC-20 token (e.g., AMPL, stETH in rebase mode) is added as a supported ckERC20 token via NNS governance proposal, the ckERC20 supply becomes permanently decoupled from the actual collateral:

- **Negative rebase scenario**: The minter holds fewer tokens than the ckERC20 supply represents. Some users cannot redeem their ckERC20 for the underlying ERC-20 — direct loss of funds for last-to-withdraw users.
- **Positive rebase scenario**: Excess tokens are locked forever in the minter's Ethereum address with no governance mechanism to recover them.

The `erc20_sub` call in `Erc20Balances` will panic on underflow if the tracked balance is exceeded, which would trap the minter canister during withdrawal processing: [6](#0-5) 

---

### Likelihood Explanation

The ckERC20 system is explicitly designed to support arbitrary ERC-20 tokens added via NNS governance proposals. There is no on-chain or off-chain enforcement preventing a rebasing token from being added. The `add_ckerc20_token` path performs no validation of the token's transfer semantics. The vulnerability is latent and becomes active the moment any rebasing token is whitelisted — a realistic governance action given the breadth of ERC-20 tokens on Ethereum mainnet.

---

### Recommendation

1. **In the helper contracts**: After `safeTransferFrom`, measure the actual balance delta at `minterAddress` and emit that as the `amount` in the event, rather than the caller-supplied parameter. This is the same `safeTransferFrom` pattern the THORChain router used for fee-on-transfer tokens.

2. **In the minter canister**: Periodically reconcile `erc20_balances` against the actual on-chain ERC-20 balance via `eth_call` (using the EVM RPC canister). Flag discrepancies and halt minting/withdrawals for affected tokens until resolved.

3. **Token admission**: Add an explicit check or documentation requirement that rebasing tokens must not be added as supported ckERC20 tokens, or implement a token-type registry that gates admission.

---

### Proof of Concept

1. AMPL (or any rebasing token) is added as a supported ckERC20 token via NNS proposal.
2. Alice deposits 1,000 AMPL via `depositErc20`. The helper emits `amount = 1000`. The minter mints 1,000 ckAMPL. `erc20_balances[AMPL] = 1000`.
3. A negative rebase occurs on Ethereum: AMPL supply contracts by 50%. The minter's actual AMPL balance is now 500. `erc20_balances[AMPL]` still reads 1000.
4. Alice calls `withdraw_erc20` for 1,000 ckAMPL. The minter burns 1,000 ckAMPL from the ledger and constructs an Ethereum transaction to send 1,000 AMPL to Alice's address.
5. The Ethereum transaction fails — the minter only holds 500 AMPL. The minter reimburses Alice's ckAMPL, but `erc20_balances` is now inconsistent.
6. Bob, who deposited 500 AMPL before the rebase (tracked as 500 ckAMPL), attempts to withdraw. The minter sends a transaction for 500 AMPL — but the minter only holds 500 AMPL total. If Alice's reimbursement race condition resolves first, Bob's withdrawal also fails. The system is permanently undercollateralized.

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-81)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
```

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

**File:** rs/ethereum/cketh/minter/src/state.rs (L729-770)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct Erc20Balances {
    balance_by_erc20_contract: BTreeMap<Address, Erc20Value>,
}

impl Erc20Balances {
    pub fn balance_of(&self, erc20_contract: &Address) -> Erc20Value {
        *self
            .balance_by_erc20_contract
            .get(erc20_contract)
            .unwrap_or(&Erc20Value::ZERO)
    }

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
