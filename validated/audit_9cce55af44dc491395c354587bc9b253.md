### Title
ckERC20 Minter Mints Based on Event-Reported Amount, Not Actual Received Amount, Enabling Ledger Over-Issuance for Fee-on-Transfer Tokens - (File: rs/ethereum/cketh/minter/src/deposit.rs)

---

### Summary

The ckERC20 minter's deposit accounting trusts the `amount` field emitted in the `ReceivedEthOrErc20` / `ReceivedErc20` Ethereum log event rather than verifying the actual ERC-20 balance received by the minter's Ethereum address. For fee-on-transfer ERC-20 tokens, the event-reported amount exceeds the actual tokens received, causing the minter to mint more ckERC20 on the IC than the ERC-20 tokens it holds. This breaks the 1:1 backing invariant and eventually prevents later withdrawers from redeeming their ckERC20 tokens.

---

### Finding Description

**Deposit flow (Ethereum side):**

The helper contract `CkDeposit.depositErc20` calls `safeTransferFrom(msg.sender, minterAddress, amount)` and then emits the event using the caller-supplied `amount` parameter — not the actual balance delta received by the minter:

```solidity
// DepositHelperWithSubaccount.sol lines 511-532
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
        amount          // ← requested amount, not actual received
    );
    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        amount,         // ← same requested amount in the event
        principal,
        subaccount
    );
}
```

For a fee-on-transfer token (e.g., one that deducts 1% on every `transferFrom`), `minterAddress` receives `amount * 0.99`, but the event records `amount`.

**IC minter side — minting:**

The minter scrapes the log, parses `ReceivedErc20Event.value` directly from the event's `amount` field, and mints exactly that value to the user:

```rust
// rs/ethereum/cketh/minter/src/deposit.rs lines 73-81
let block_index = match client
    .transfer(TransferArg {
        from_subaccount: None,
        to: event.beneficiary(),
        fee: None,
        created_at_time: None,
        memo: Some((&event).into()),
        amount: event.value(),   // ← event-reported amount, not actual received
    })
    .await
```

**IC minter side — balance tracking:**

The internal `erc20_balances` ledger is also updated with the event-reported value:

```rust
// rs/ethereum/cketh/minter/src/state.rs lines 332-338
fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
    match event {
        ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
        ReceivedEvent::Erc20(event) => self
            .erc20_balances
            .erc20_add(event.erc20_contract_address, event.value),  // ← inflated
    };
}
```

**Withdrawal side:**

When a user calls `withdraw_erc20`, the minter burns `ckerc20_withdrawal_amount` from the ckERC20 ledger and constructs an Ethereum `ERC20.transfer(destination, withdrawal_amount)` call. If the minter's actual on-chain ERC-20 balance is less than the sum of all outstanding ckERC20 (due to accumulated fee shortfalls), the Ethereum transaction will revert (out-of-funds), and the user's ckERC20 tokens are burned but no ERC-20 is received. The minter reimburses ckERC20 only on Ethereum-level transaction failure, but if the ERC-20 `transfer` call itself reverts inside the EVM, the Ethereum transaction status is `Failure` and the minter does reimburse — however, the minter's ETH gas fee is still consumed and not reimbursed, and the systemic undercollateralization persists for all future withdrawers.

The `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is fixed at `65_000`:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs line 44
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

This fixed gas limit also means that if the fee-on-transfer token's `transfer` function consumes more gas than standard (e.g., due to fee logic), the transaction may run out of gas, causing a failed withdrawal that still burns the user's ckETH gas fee.

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn accounting bug / ledger conservation bug.

For every deposit of a fee-on-transfer ERC-20 token, the minter mints `amount` ckERC20 but only holds `amount - fee` ERC-20. Over N deposits of size `A` with fee rate `f`:

- Total ckERC20 minted: `N * A`
- Total ERC-20 held: `N * A * (1 - f)`
- Shortfall: `N * A * f`

The last `N * A * f / A = N * f` equivalent users to withdraw will find the minter insolvent. Their ckERC20 tokens are burned on the IC ledger but the Ethereum withdrawal fails. The minter reimburses ckERC20 on Ethereum transaction failure, but the systemic shortfall means the minter's Ethereum address genuinely lacks the tokens — the reimbursement loop does not resolve the undercollateralization, it only returns the ckERC20 to the user. The ERC-20 tokens are permanently locked in the minter's Ethereum address in a quantity insufficient to cover all outstanding ckERC20.

**Concrete impact:** Permanent loss of funds for a subset of ckERC20 holders; the 1:1 backing invariant of the chain-fusion bridge is broken.

---

### Likelihood Explanation

**Medium.** The attack requires a fee-on-transfer ERC-20 token to be added as a supported ckERC20 token via NNS governance proposal. This is not a purely unprivileged path, but:

1. The NNS community may not audit every token's transfer behavior before voting.
2. Some tokens (e.g., upgradeable proxies) can add transfer fees *after* being listed as a supported ckERC20 token.
3. The documentation explicitly states: *"Any ERC-20 token on Ethereum can be brought to the Internet Computer by adding a new ckERC20 token"* — there is no on-chain enforcement preventing fee-on-transfer tokens from being added.
4. Once a fee-on-transfer token is listed, any unprivileged user depositing it triggers the accounting error with each deposit.

---

### Recommendation

1. **Measure actual received amount:** In the Solidity helper contracts, record the minter's ERC-20 balance before and after `safeTransferFrom` and emit the delta as the event amount:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = IERC20(erc20Address).balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

2. **Token validation in NNS proposals:** Before adding a new ckERC20 token, the proposal process should verify the token does not implement transfer fees, rebasing, or blocklists. This check should be documented as a mandatory step.

3. **Gas limit validation:** The fixed `65_000` gas limit should be validated against the actual gas consumption of the specific ERC-20 token's `transfer` function before listing.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token (e.g., 1% fee on every transfer) is added as a supported ckERC20 token via NNS proposal.

2. User calls `depositErc20(feeToken, 1_000_000, principal, subaccount)` on the helper contract.

3. Helper contract calls `feeToken.safeTransferFrom(user, minter, 1_000_000)`. Due to the 1% fee, minter receives `990_000` tokens. The event emits `amount = 1_000_000`.

4. IC minter scrapes the log, reads `value = 1_000_000` from `ReceivedErc20Event`: [1](#0-0) 

5. Minter calls `icrc1_transfer` with `amount: event.value()` = `1_000_000`, minting `1_000_000` ckFeeToken to the user: [2](#0-1) 

6. Internal `erc20_balances` records `1_000_000` for this token, but actual on-chain balance is `990_000`: [3](#0-2) 

7. After 100 such deposits: ckFeeToken total supply = `100_000_000`; minter's actual ERC-20 balance = `99_000_000`. Shortfall = `1_000_000`.

8. The 100th user to withdraw `1_000_000` ckFeeToken triggers an Ethereum `transfer(destination, 1_000_000)` call. The minter's balance is exactly `0` at this point (assuming all others withdrew first), so the ERC-20 `transfer` reverts. The Ethereum transaction status is `Failure`: [4](#0-3) 

9. The minter reimburses the ckERC20 tokens to the user, but the minter's Ethereum address holds `0` ERC-20 tokens — the shortfall is permanent and unrecoverable without external intervention.

The helper contract emitting the caller-supplied `amount` rather than the actual received balance is the root cause: [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L57-75)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Decode, Encode)]
pub struct ReceivedErc20Event {
    #[n(0)]
    pub transaction_hash: Hash,
    #[n(1)]
    pub block_number: BlockNumber,
    #[cbor(n(2))]
    pub log_index: LogIndex,
    #[n(3)]
    pub from_address: Address,
    #[n(4)]
    pub value: Erc20Value,
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub principal: Principal,
    #[n(6)]
    pub erc20_contract_address: Address,
    #[n(7)]
    pub subaccount: Option<LedgerSubaccount>,
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-745)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

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
