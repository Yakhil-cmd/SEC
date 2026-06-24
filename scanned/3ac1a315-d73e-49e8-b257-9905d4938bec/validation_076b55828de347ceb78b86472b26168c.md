### Title
ETH/ERC20 Permanently Locked in Minter on Invalid `bytes32` Principal Encoding — (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

### Summary

When a user deposits ETH or ERC20 tokens via the ckETH helper contract on Ethereum with an incorrectly encoded `bytes32` principal, the funds are irreversibly transferred to the minter's Ethereum address but no ckETH/ckERC20 is ever minted. There is no recovery mechanism in the minter canister, resulting in permanent fund loss. This is the IC chain-fusion analog of the zkSync address-aliasing class: in both cases a cross-chain recipient identifier is processed in a way the user does not expect, and the deposited funds end up in an address/account the user cannot access.

---

### Finding Description

The ckETH minter scrapes `ReceivedEth` and `ReceivedEthOrErc20` events from the Ethereum helper contracts. For every event the minter calls `parse_principal_from_slice` on the 32-byte principal field. [1](#0-0) 

The encoding contract is strict:
- Byte 0 = length N (1–29)
- Bytes 1…N = raw IC principal bytes **without** the CRC32 checksum
- Bytes N+1…31 = must all be zero

If any of these conditions are violated the function returns `Err`, the minter emits `EventType::InvalidDeposit`, and the event is stored in `invalid_events` forever. [2](#0-1) 

The ETH or ERC20 has already been transferred to the minter's Ethereum address (controlled by tECDSA) at the time the Ethereum transaction was mined. The minter exposes no admin endpoint to recover or redirect those funds. The `invalid_events` map is read-only from the outside. [3](#0-2) 

The helper contract itself performs no on-chain validation of the `bytes32` principal before accepting the ETH/ERC20: [4](#0-3) [5](#0-4) 

The encoding is non-standard and requires specific tooling. Common user mistakes include:

| Mistake | Result |
|---|---|
| Including the 4-byte CRC32 checksum in the principal bytes | `trailing non-zero bytes` error → `InvalidDeposit` |
| Wrong length byte (e.g., off-by-one) | Parses a different principal or `trailing non-zero bytes` error |
| Using a raw base32-decoded principal without stripping the checksum | Same as above |

The official JS helper strips the checksum correctly: [6](#0-5) 

But the Ethereum smart contract accepts any 32-byte value without validation, so any user who does not use the official tooling can silently lock their funds.

The documentation acknowledges the risk but provides no on-chain guard: [7](#0-6) 

---

### Impact Explanation

ETH or ERC20 tokens are permanently locked in the minter's Ethereum address. The minter canister has no function to return them to the depositor. Because the minter's Ethereum key is a threshold ECDSA key shared across the subnet, no individual node or operator can unilaterally move the funds either. The only remediation path is a governance-approved canister upgrade that adds a recovery endpoint — a slow, high-friction process that may never happen for small amounts.

---

### Likelihood Explanation

Medium. The `bytes32` encoding is non-standard and underdocumented outside the official dashboard. Any Ethereum-native developer who constructs the call manually (e.g., via Etherscan's write-contract UI, a custom script, or a third-party bridge) and does not use the official `principal_to_hex` binary or JS helper is at risk. The test suite itself demonstrates the failure mode: [8](#0-7) 

The `invalid_principal` value in that test (`0x0a01f79d0000000000fe01...`) is a plausible encoding mistake (length byte correct, but trailing bytes non-zero due to checksum inclusion).

---

### Recommendation

1. **Add a recovery endpoint** to the minter canister (governance-gated) that, given an `EventSource` (tx hash + log index) recorded in `invalid_events`, signs and broadcasts an Ethereum transaction returning the locked ETH/ERC20 to the `from_address` recorded in the original log entry.

2. **Add on-chain validation** in the Solidity helper contracts to reject calls where the `bytes32` principal field does not satisfy the length-prefix invariant (first byte ≤ 29, trailing bytes zero), reverting before any ETH/ERC20 is transferred.

3. **Emit a recoverable event** on the IC side (rather than silently recording `InvalidDeposit`) so that monitoring systems can alert depositors immediately.

---

### Proof of Concept

1. User calls `depositEth(bytes32 principal, bytes32 subaccount)` on `CkDeposit` with a principal encoded **including** the 4-byte CRC32 checksum, e.g.:

   ```
   principal = 0x0d<crc32_4bytes><raw_principal_9bytes><zeros>
   ```

   The first byte `0x0d` = 13 (correct total length including checksum), but the trailing bytes after the 9 raw principal bytes are non-zero (the checksum bytes bleed into the trailing region).

2. The ETH is transferred to the minter's address on Ethereum (irreversible).

3. The minter scrapes the `ReceivedEthOrErc20` event and calls `parse_principal_from_slice` on the 32-byte topic.

4. `parse_principal_from_slice` finds non-zero trailing bytes and returns `Err("trailing non-zero bytes")`. [9](#0-8) 

5. `register_deposit_events` emits `EventType::InvalidDeposit` and stores the source in `state.invalid_events`. [10](#0-9) 

6. No ckETH is ever minted. The ETH is permanently locked in the minter's Ethereum address. No canister endpoint exists to recover it.

### Citations

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L247-291)
```rust
/// Decode a candid::Principal from a slice of at most 32 bytes
/// encoded as follows
/// - the first byte is the number of bytes in the principal
/// - the next N bytes are the principal
/// - the remaining bytes are zero
///
/// Any other encoding will return an error.
/// Some specific valid [`Principal`]s are also not allowed
/// since the decoded principal will be used to receive ckETH:
/// * the management canister principal
/// * the anonymous principal
///
/// This method MUST never panic (decode bytes from untrusted sources).
fn parse_principal_from_slice(slice: &[u8]) -> Result<Principal, String> {
    const ANONYMOUS_PRINCIPAL_BYTES: [u8; 1] = [4];

    if slice.is_empty() {
        return Err("slice too short".to_string());
    }
    if slice.len() > 32 {
        return Err(format!("Expected at most 32 bytes, got {}", slice.len()));
    }
    let num_bytes = slice[0] as usize;
    if num_bytes == 0 {
        return Err("management canister principal is not allowed".to_string());
    }
    if num_bytes > 29 {
        return Err(format!(
            "invalid number of bytes: expected a number in the range [1,29], got {num_bytes}",
        ));
    }
    if slice.len() < 1 + num_bytes {
        return Err("slice too short".to_string());
    }
    let (principal_bytes, trailing_zeroes) = slice[1..].split_at(num_bytes);
    if !trailing_zeroes
        .iter()
        .all(|trailing_zero| *trailing_zero == 0)
    {
        return Err("trailing non-zero bytes".to_string());
    }
    if principal_bytes == ANONYMOUS_PRINCIPAL_BYTES {
        return Err("anonymous principal is not allowed".to_string());
    }
    Principal::try_from_slice(principal_bytes).map_err(|err| err.to_string())
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L311-360)
```rust
pub fn register_deposit_events(
    scraping_id: LogScrapingId,
    transaction_events: Vec<ReceivedEvent>,
    errors: Vec<ReceivedEventError>,
) {
    for event in transaction_events {
        log!(
            INFO,
            "Received event {event:?}; will mint {} {scraping_id} to {}",
            event.value(),
            event.beneficiary()
        );
        if crate::blocklist::is_blocked(&event.from_address()) {
            log!(
                INFO,
                "Received event from a blocked address: {} for {} {scraping_id}",
                event.from_address(),
                event.value(),
            );
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
    }
    if read_state(State::has_events_to_mint) {
        ic_cdk_timers::set_timer(Duration::from_secs(0), async { mint().await });
    }
    for error in errors {
        if let ReceivedEventError::InvalidEventSource { source, error } = &error {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: *source,
                        reason: error.to_string(),
                    },
                )
            });
        }
        report_transaction_error(error);
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L64-66)
```rust
    pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
    pub minted_events: BTreeMap<EventSource, MintedEvent>,
    pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/templates/principal_to_bytes.js (L65-73)
```javascript
    let ungroup = text.replace(/-/g, "");
    let rawBytes = base32Decode(ungroup);
    if (rawBytes.length < 4) {
        throw Error("Invalid principal: too short");
    }
    if (rawBytes.length > 33) {
        throw Error("Invalid principal: too long");
    }
    return bytes32Encode(rawBytes.slice(4));
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L115-119)
```text
[WARNING]
====
* It's critical that the encoded IC principal is correct otherwise the funds will be lost.
* The helper smart contracts for Ethereum and for Sepolia have different addresses (refer to the above table).
====
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L1591-1611)
```rust
fn should_block_deposit_from_corrupted_principal() {
    let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
    let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
    let invalid_principal = "0x0a01f79d0000000000fe01000000000000000000000000000000000000000001";

    ckerc20
        .deposit(DepositCkErc20Params::new(ONE_USDC, ckusdc))
        .with_override_erc20_log_entry(|mut entry| {
            entry.topics[3] = invalid_principal.parse().unwrap();
            entry
        })
        .expect_no_mint()
        .check_events()
        .assert_has_unique_events_in_order(&[EventPayload::InvalidDeposit {
            event_source: EventSource {
                transaction_hash: DEFAULT_ERC20_DEPOSIT_TRANSACTION_HASH.to_string(),
                log_index: Nat::from(DEFAULT_ERC20_DEPOSIT_LOG_INDEX),
            },
            reason: format!("failed to decode principal from bytes {invalid_principal}"),
        }]);
}
```
