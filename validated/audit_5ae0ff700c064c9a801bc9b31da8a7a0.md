I have enough information to write the full report. The vulnerability is clearly present in both ERC20 deposit helper contracts.

---

### Title
Missing Received-Amount Verification in ERC20 Deposit Helper Contracts Enables ckERC20 Over-Minting — (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol` and `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

Both ERC20 deposit helper smart contracts (`CkErc20Deposit.deposit()` and `CkDeposit.depositErc20()`) call `safeTransferFrom` and then immediately emit a deposit event carrying the caller-supplied `amount` parameter — without checking the minter address's actual before/after token balance. The IC ckETH minter canister scrapes these events and mints ckERC20 tokens equal to the logged `amount`. For any fee-on-transfer or rebasing ERC20 token where the actual received amount is less than `amount`, the minter mints more ckERC20 than the ERC20 tokens it actually holds, breaking the 1:1 backing invariant.

---

### Finding Description

**`CkErc20Deposit.deposit()` in `ERC20DepositHelper.sol`:**

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    // ❌ No before/after balance check — emits caller-supplied `amount`
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

**`CkDeposit.depositErc20()` in `DepositHelperWithSubaccount.sol`:**

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    // ❌ No before/after balance check — emits caller-supplied `amount`
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [2](#0-1) 

The IC minter canister's `mint()` function in `rs/ethereum/cketh/minter/src/deposit.rs` then reads the scraped event and mints exactly `event.value()` ckERC20 tokens — which is the `amount` field from the event log, not the actual tokens received:

```rust
let block_index = match client
    .transfer(TransferArg {
        amount: event.value(),   // ← sourced directly from the event log `amount`
        ...
    })
    .await
``` [3](#0-2) 

There is no cross-check between the logged `amount` and the actual ERC20 balance increase at the minter's Ethereum address.

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug — ledger conservation violation.

For any supported ERC20 token that implements fee-on-transfer semantics (e.g., USDT's dormant fee mechanism, or any future supported token with such behavior), the minter's Ethereum address receives `amount - fee` tokens while the IC ckERC20 ledger mints `amount` tokens. Over repeated deposits, the total ckERC20 supply exceeds the actual ERC20 collateral held by the minter. When users later withdraw ckERC20 back to ERC20, the minter cannot fulfill all redemptions — the last withdrawers receive nothing, constituting a direct loss of funds.

The `Erc20Balances` internal accounting in the minter state also tracks the inflated `amount`: [4](#0-3) 

This means the minter's own balance tracking diverges from the real on-chain ERC20 balance, compounding the accounting error.

---

### Likelihood Explanation

**Medium.** The minter enforces a whitelist of supported ERC20 tokens via NNS governance proposals. Standard tokens currently supported (e.g., USDC) do not have active transfer fees. However:

1. USDT (Tether) has a fee mechanism in its contract that is currently set to zero but can be enabled by the Tether issuer at any time without any IC governance action.
2. Any future NNS proposal to add a new ckERC20 token for a fee-on-transfer token would immediately expose this path.
3. Rebasing tokens (supply-decreasing rebase) present the same risk.

The attacker-controlled entry path is fully unprivileged: any Ethereum user can call `depositErc20` on the helper contract with a supported fee-on-transfer ERC20 token. No special role or key is required.

---

### Recommendation

In both helper contracts, capture the minter's actual balance before and after the `safeTransferFrom` call, and emit the difference as the deposit amount:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
    require(actualReceived > 0, "ERC20: no tokens received");
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
}
```

Apply the same fix to `CkErc20Deposit.deposit()` in `ERC20DepositHelper.sol`. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

1. A supported ERC20 token (e.g., USDT with fee enabled, or a newly added fee-on-transfer token) is used.
2. Attacker calls `depositErc20(usdtAddress, 1_000_000, principal, subaccount)` on `CkDeposit`.
3. `safeTransferFrom` transfers only `990_000` tokens to the minter (1% fee deducted).
4. The contract emits `ReceivedEthOrErc20(..., amount=1_000_000, ...)`.
5. The IC minter scrapes the event and calls `mint()` with `event.value() = 1_000_000`.
6. The user receives `1_000_000` ckERC20 tokens on the IC ledger.
7. The minter's Ethereum address holds only `990_000` ERC20 tokens.
8. Repeating this inflates ckERC20 supply beyond actual collateral, eventually making the system insolvent for the last redeemers. [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L495-503)
```text
    /**
     * @dev Emits the `ReceivedErc20` event if the transfer succeeds.
     */
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L508-532)
```text
    /**
     * @dev Emits the `ReceivedEthOrErc20` event if the transfer succeeds.
     */
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L40-128)
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
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
        mutate_state(|s| {
            process_event(
                s,
                match &event {
                    ReceivedEvent::Eth(event) => EventType::MintedCkEth {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                    },

                    ReceivedEvent::Erc20(event) => EventType::MintedCkErc20 {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                        erc20_contract_address: event.erc20_contract_address,
                        ckerc20_token_symbol: token_symbol.clone(),
                    },
                },
            )
        });
        log!(
            INFO,
            "Minted {} {token_symbol} to {} in block {block_index}",
            event.value(),
            event.beneficiary()
        );
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L742-755)
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
```
