### Title
Fee-on-Transfer (Deflation) ERC20 Token Support Causes ckERC20 Over-Minting, Breaking 1:1 Backing Invariant - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Both ckERC20 deposit helper smart contracts emit the deposit event using the caller-supplied `amount` parameter rather than the actual amount received by the minter address after `safeTransferFrom`. The IC minter canister reads these events and mints exactly `event.value()` ckERC20 tokens. For any fee-on-transfer (deflation) ERC20 token, the minter receives less than `amount` but mints the full `amount`, permanently over-issuing ckERC20 relative to the actual ERC20 backing held.

---

### Finding Description

The deposit flow for ERC20 → ckERC20 passes through two helper contracts. In `CkErc20Deposit.deposit()`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

And in `depositErc20()` of the newer helper:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
``` [2](#0-1) 

In both cases, the event is emitted with the **input `amount`**, not the balance delta actually credited to `minterAddress`. For a fee-on-transfer token, `safeTransferFrom(sender, minter, amount)` results in the minter receiving `amount - fee`, but the event records `amount`.

The IC minter canister scrapes these logs and unconditionally mints `event.value()` ckERC20 tokens:

```rust
amount: event.value(),  // taken directly from the event log
``` [3](#0-2) 

There is no balance-before/balance-after check anywhere in the deposit path. The minter has no independent way to verify the actual received amount — it is entirely dependent on the event log value. [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug — ledger conservation invariant broken.

For every deposit of a fee-on-transfer ERC20 token, the ckERC20 ledger total supply grows by `amount` while the minter's actual ERC20 reserve grows by only `amount - fee`. This gap accumulates with every deposit. Eventually, the minter's ERC20 balance is insufficient to honor all outstanding ckERC20 redemptions. The last withdrawers are unable to redeem their ckERC20 tokens for the underlying ERC20, resulting in permanent loss of funds for those users. The ckERC20 peg is broken.

---

### Likelihood Explanation

The ckERC20 supported token list is managed by NNS governance proposals. Any ERC20 token with a fee-on-transfer mechanism (e.g., tokens with built-in redistribution, reflection, or burn-on-transfer) could be proposed and added. Once added, every ordinary user deposit silently over-mints. No special attacker capability is required beyond submitting a normal deposit transaction to the helper contract. The vulnerability is triggered by any unprivileged Ethereum user interacting with the helper contract for a supported fee-on-transfer token.

---

### Recommendation

In both helper contracts, measure the actual balance delta received by the minter address and emit that value in the event, rather than the caller-supplied `amount`:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

Additionally, the minter's NNS-governed token onboarding process should explicitly screen out fee-on-transfer tokens, and the documentation should state that such tokens are not supported.

---

### Proof of Concept

1. An NNS proposal adds a fee-on-transfer ERC20 token (e.g., 1% burn-on-transfer) to the supported ckERC20 list.
2. A user calls `depositErc20(feeToken, 1_000_000, principal, subaccount)` on `DepositHelperWithSubaccount`.
3. `safeTransferFrom(user, minter, 1_000_000)` executes; the token burns 1% in-flight, so the minter receives `990_000` tokens.
4. The contract emits `ReceivedEthOrErc20(feeToken, user, 1_000_000, principal, subaccount)` — logging `1_000_000`. [5](#0-4) 
5. The IC minter scrapes the log, reads `event.value() = 1_000_000`, and mints `1_000_000` ckFeeToken to the user. [3](#0-2) 
6. After 100 such deposits, the minter holds `99_000_000` ERC20 tokens but has issued `100_000_000` ckFeeToken. The last ~1% of ckFeeToken holders cannot withdraw, losing their funds.

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L519-531)
```text
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L40-82)
```rust
    for event in events {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this event will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::QuarantinedDeposit {
                        event_source: event.source(),
                    },
                )
            });
        });
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
            }
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
```
