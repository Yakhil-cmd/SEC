### Title
ckERC20 Minter Mints Tokens Based on Log-Event Amount Rather Than Actual Received Balance, Enabling Unbacked ckERC20 Minting for Fee-on-Transfer ERC20 Tokens — (Files: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter's ERC20 deposit helper contracts emit the `ReceivedErc20` / `ReceivedEthOrErc20` event using the caller-supplied `amount` argument, not the actual token balance received by the minter's Ethereum address. The IC minter canister scrapes these logs and mints ckERC20 tokens equal to `event.value()` — the logged amount — without any on-chain or off-chain verification that the minter's Ethereum address actually received that quantity. For any ERC20 token with fee-on-transfer semantics, the minter would mint more ckERC20 than the ERC20 tokens it holds, creating an unbacked supply and breaking ledger conservation.

---

### Finding Description

**Step 1 — Helper contract emits the input amount, not the received amount.**

In `ERC20DepositHelper.sol`:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

And in `DepositHelperWithSubaccount.sol` (`CkDeposit.depositErc20`):

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
``` [2](#0-1) 

Both contracts emit the event with the **input `amount`** parameter. For a fee-on-transfer ERC20 token, `safeTransferFrom` delivers `amount - fee` to `minterAddress`, but the event records `amount`. The Ethereum log is the sole source of truth for the IC minter.

**Step 2 — IC minter parses the log value and mints without balance verification.**

The log parser extracts `value` directly from the event data field:

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
Ok(ReceivedErc20Event {
    value: Erc20Value::from_be_bytes(value_bytes),
    ...
}.into())
``` [3](#0-2) 

The `mint()` function in `deposit.rs` then calls the ICRC-1 ledger with `amount: event.value()`:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),
    })
    .await
``` [4](#0-3) 

There is no step that queries the minter's actual Ethereum ERC20 balance before or after minting to confirm the received amount matches the logged amount.

**Step 3 — No supported-token whitelist enforcement at the contract level.**

The helper contracts do not restrict which ERC20 tokens can be deposited:

```solidity
require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
``` [5](#0-4) 

The minter filters by supported tokens only at the IC log-scraping level (topic filtering), but the Ethereum contract itself accepts any ERC20.

---

### Impact Explanation

If any supported ckERC20 token has fee-on-transfer behavior (now or after an ERC20 contract upgrade), every deposit mints more ckERC20 than the ERC20 tokens held by the minter's Ethereum address. This:

- **Breaks ledger conservation**: total ckERC20 supply exceeds the ERC20 collateral held on Ethereum.
- **Causes withdrawal failures**: when later users attempt to withdraw ckERC20 → ERC20, the minter's Ethereum address will have insufficient ERC20 balance to fulfill all outstanding ckERC20 claims.
- **Enables profit extraction**: an attacker who deposits a fee-on-transfer token receives more ckERC20 than the ERC20 value they deposited, effectively extracting value from the protocol's reserves.

This is a **chain-fusion mint/burn accounting bug** — the canonical IC analog of the ZKsync `L1NativeTokenVault` vulnerability.

---

### Likelihood Explanation

Currently supported ckERC20 tokens (USDC, USDT, etc.) do not implement fee-on-transfer. However:

1. The protocol has no enforcement at the Solidity contract level preventing fee-on-transfer tokens from being deposited.
2. Some ERC20 tokens are upgradeable; a future upgrade to a supported token could introduce fee-on-transfer behavior without the IC minter being aware.
3. A future NNS governance proposal could add a token that has fee-on-transfer semantics, either intentionally or by mistake, since the minter code does not validate this property.
4. The `deposit.rs` `mint()` function is called on a timer for all `events_to_mint()` — there is no per-event balance reconciliation. [6](#0-5) 

Likelihood is **low-medium**: not exploitable today with current supported tokens, but the structural gap is present and would be triggered automatically if the condition is ever met.

---

### Recommendation

1. **In the Solidity helper contracts**: record the actual received amount by checking the minter's balance before and after the `safeTransferFrom`, and emit the delta as the event value:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 received = IERC20(erc20Address).balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, received, principal, subaccount);
```

2. **In the IC minter**: when adding a new ckERC20 token via `AddCkErc20Token`, perform a validation deposit (or require a governance attestation) confirming the token does not have fee-on-transfer behavior.

3. **Alternatively**: document and enforce that only non-fee-on-transfer tokens may be added as supported ckERC20 tokens, and add an explicit check in the NNS proposal validation path.

---

### Proof of Concept

**Scenario**: A fee-on-transfer ERC20 token (10% fee) is a supported ckERC20 token.

1. User approves the helper contract for 1000 tokens.
2. User calls `depositErc20(feeToken, 1000, principal, subaccount)`.
3. Helper contract calls `safeTransferFrom(user, minter, 1000)` — minter receives 900 tokens (10% fee burned).
4. Helper contract emits `ReceivedEthOrErc20(feeToken, user, 1000, principal, subaccount)`.
5. IC minter scrapes the log, parses `value = 1000`.
6. IC minter calls `icrc1_transfer({ amount: 1000, to: user_account })` on the ckERC20 ledger.
7. User now holds 1000 ckERC20 but the minter's Ethereum address holds only 900 ERC20 tokens.
8. Repeat: after 10 such deposits the minter holds 9000 ERC20 but has issued 10000 ckERC20.
9. The 10th user to withdraw will find the minter's Ethereum address has insufficient ERC20 balance.

The attacker-controlled entry path is a direct unprivileged call to the public `depositErc20` function on the deployed Ethereum helper contract, which triggers the IC minter's timer-based log scraping and minting without any additional privilege. [7](#0-6) [8](#0-7) [1](#0-0) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L69-103)
```rust
impl LogParser for ReceivedErc20LogParser {
    fn parse_log(entry: LogEntry) -> Result<ReceivedEvent, ReceivedEventError> {
        let (block_number, event_source) = ensure_not_pending(&entry)?;
        ensure_not_removed(&entry, event_source)?;

        ensure_topics(
            &entry,
            |topics| {
                topics.len() == 4
                    && topics.first() == Some(&Hex32::from(RECEIVED_ERC20_EVENT_TOPIC))
            },
            event_source,
        )?;
        let erc20_contract_address = parse_address(&entry.topics[1], event_source)?;
        let from_address = parse_address(&entry.topics[2], event_source)?;
        let principal = parse_principal(&entry.topics[3], event_source)?;

        let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value: Erc20Value::from_be_bytes(value_bytes),
            principal,
            erc20_contract_address,
            subaccount: None,
        }
        .into())
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L28-138)
```rust
async fn mint() {
    use icrc_ledger_client_cdk::{CdkRuntime, ICRC1Client};
    use icrc_ledger_types::icrc1::transfer::TransferArg;

    let _guard = match TimerGuard::new(TaskType::Mint) {
        Ok(guard) => guard,
        Err(_) => return,
    };

    let (eth_ledger_canister_id, events) = read_state(|s| (s.cketh_ledger_id, s.events_to_mint()));
    let mut error_count = 0;

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
    }

    if error_count > 0 {
        log!(
            INFO,
            "Failed to mint {error_count} events, rescheduling the minting"
        );
        ic_cdk_timers::set_timer(crate::MINT_RETRY_DELAY, async { mint().await });
    }
}
```
