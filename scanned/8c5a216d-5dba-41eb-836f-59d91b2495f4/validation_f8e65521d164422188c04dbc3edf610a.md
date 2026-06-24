### Title
ckERC20 Minter Over-Mints Tokens for Fee-on-Transfer ERC-20 Deposits Due to Trusting Event-Emitted Amount - (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter mints ckERC20 tokens based on the `value` field scraped from the `ReceivedErc20` / `ReceivedEthOrErc20` Ethereum log event. Both helper smart contracts emit the caller-supplied `amount` parameter — not the actual tokens received by the minter address. For fee-on-transfer ERC-20 tokens, the minter receives `amount - fee` but mints `amount` of ckERC20, making the ckERC20 supply permanently undercollateralized.

---

### Finding Description

The two Ethereum-side helper contracts emit the deposit event with the **requested** `amount`, not the **actual received** amount:

**`ERC20DepositHelper.sol`** (`deposit` function):
```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal); // emits requested amount
``` [1](#0-0) 

**`DepositHelperWithSubaccount.sol`** (`depositErc20` function):
```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount); // emits requested amount
``` [2](#0-1) 

The IC minter scrapes these logs, stores the `value` field from the event into `ReceivedErc20Event.value`: [3](#0-2) 

And then mints exactly `event.value()` of ckERC20 to the beneficiary without any balance-before/after verification:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),  // trusts the log-emitted amount
    })
    .await
``` [4](#0-3) 

For a fee-on-transfer ERC-20 token (e.g., a token that deducts 1% on every `transferFrom`), the minter's Ethereum address receives `amount * 0.99`, but the IC minter mints `amount` of ckERC20. The 1% gap is unbacked.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion mint accounting bug.**

Each deposit of a fee-on-transfer ERC-20 token inflates the ckERC20 supply beyond the actual ERC-20 collateral held by the minter. When users later attempt to withdraw ckERC20 back to ERC-20, the minter's Ethereum balance is insufficient to cover all outstanding ckERC20. The last withdrawers receive less ERC-20 than their ckERC20 represents, or withdrawals fail entirely. The ckERC20 token loses its 1:1 peg guarantee.

**Impact: Medium** — ckERC20 undercollateralization; withdrawal failures for some users; loss of funds proportional to the fee rate and total deposited volume.

---

### Likelihood Explanation

**Likelihood: Medium.**

The minter maintains a whitelist of supported ERC-20 tokens. Currently deployed tokens (USDC, USDT, LINK, etc.) do not have transfer fees active. However:

1. **USDT has a fee mechanism in its contract** that is currently set to zero but can be enabled by the USDT issuer at any time without the IC minter being aware.
2. Any future NNS proposal to add a new ckERC20 token that happens to be fee-on-transfer would silently trigger this bug.
3. The entry path requires no special privileges — any unprivileged user calling `depositErc20` on the helper contract with a fee-on-transfer token triggers the over-mint.

The attacker-controlled entry path is: call `depositErc20(feeOnTransferToken, amount, principal, subaccount)` on the helper contract → minter scrapes the log → minter mints `amount` of ckERC20 → minter only holds `amount - fee` of ERC-20.

---

### Recommendation

1. **In the Solidity helper contracts**: Record the minter's ERC-20 balance before and after `safeTransferFrom`, and emit the **actual received amount** (`balanceAfter - balanceBefore`) in the event rather than the caller-supplied `amount`.

2. **In the IC minter**: As a defense-in-depth measure, when processing a deposit event, verify the minter's on-chain ERC-20 balance is consistent with the expected collateral before minting. Alternatively, explicitly document and enforce that only non-fee-on-transfer tokens may be added as supported ckERC20 tokens, and add a check in the token-addition governance proposal validation.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token (1% fee) is added as a supported ckERC20 token via NNS proposal.
2. User calls `depositErc20(feeToken, 1_000_000, principal, subaccount)` on the helper contract.
3. Helper contract calls `safeTransferFrom(user, minter, 1_000_000)` — minter receives `990_000` tokens.
4. Helper contract emits `ReceivedErc20(feeToken, user, 1_000_000, principal)`.
5. IC minter scrapes the log, reads `value = 1_000_000`, mints `1_000_000` ckFeeToken to the user.
6. Minter holds `990_000` ERC-20 but has issued `1_000_000` ckERC20 — 10,000 tokens unbacked.
7. Repeated deposits compound the undercollateralization. When the last user tries to withdraw, the minter's ERC-20 balance is insufficient. [5](#0-4) [6](#0-5)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L40-102)
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
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L67-103)
```rust
pub enum ReceivedErc20LogParser {}

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
