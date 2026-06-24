### Title
Case-Insensitive Bech32 Bypass of Minter-Address Guard in ckBTC PEG-OUT — (`File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The ckBTC minter's `retrieve_btc` and `retrieve_btc_with_approval` endpoints contain a guard that is intended to prevent users from accidentally burning ckBTC while specifying the minter's own Bitcoin address as the withdrawal destination. This guard is implemented as a plain byte-for-byte string comparison against the minter's canonical (always-lowercase) bech32 address string. Because the minter's bech32 parser (`parse_bip173_address`) normalises the human-readable part to lowercase before accepting an address, an unprivileged caller can supply the minter's address in any mixed-case or all-uppercase form, pass the guard, have their ckBTC burned, and cause the minter to send BTC to its own treasury — with no reimbursement path.

---

### Finding Description

**Guard implementation (both endpoints):**

```
// retrieve_btc  (line 158)
if args.address == main_address_str {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

`main_address_str` is produced by `derive_minter_address_str` → `display()` → `encode_bech32()`, which always encodes with the lowercase HRP `"bc"` (mainnet). The resulting string is therefore always lowercase, e.g. `bc1q0jrxz4jh59t5qsu7l0y59kpfdmgjcq60wlee3h`.

**Parser (line 344 of `address.rs`):**

```rust
if found_hrp.to_lowercase() != expected_hrp {
    return Err(…);
}
```

The HRP comparison is case-insensitive. The bech32 data characters are also case-insensitive by the BIP-173 spec, and the `bech32` crate decodes them identically regardless of case. The existing unit test in the same file proves this explicitly:

```rust
// Both parse to the identical BitcoinAddress::P2wpkhV0([…])
BitcoinAddress::parse("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", Network::Mainnet)
BitcoinAddress::parse("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", Network::Mainnet)
```

**Consequence:** A caller who supplies the minter's address in uppercase (or any mixed-case variant) will:

1. Pass the `==` guard at line 158 / 256 (strings differ in case).
2. Pass `BitcoinAddress::parse` at line 173 / 271–273 (decoded to the same `P2wpkhV0` bytes as the minter's address).
3. Have their ckBTC burned via `burn_ckbtcs` / `burn_ckbtcs_icrc2`.
4. Have the withdrawal request queued with `address = parsed_address` — which is the minter's own address.
5. Receive no BTC; the minter sends the satoshis to itself.

The `reimbursement_account` field is only consulted when the Bitcoin transaction itself fails (e.g. insufficient fee). A successful Bitcoin send to the minter's own address is not a failure condition, so no reimbursement is triggered.

---

### Impact Explanation

An unprivileged ckBTC holder loses their entire withdrawal amount: ckBTC is irreversibly burned on the IC ledger, and the corresponding BTC is deposited into the minter's own UTXO pool. The minter's pool grows by the same amount, benefiting all remaining ckBTC holders at the expense of the victim. There is no on-chain recovery path once the Bitcoin transaction is confirmed.

---

### Likelihood Explanation

Low-to-medium. The minter's mainnet address is publicly known (dashboard, `get_minter_info`). A user could encounter the uppercase form through:
- Copy-paste from a QR code scanner or wallet that uppercases bech32 output (some hardware wallets do this).
- A malicious front-end that substitutes the minter's address in uppercase for the user's intended address.
- Deliberate self-harm (griefing).

The scenario is realistic enough to warrant a fix given the irreversible fund loss.

---

### Recommendation

Replace the plain string comparison with a comparison of the **parsed** address against the minter's parsed address:

```rust
// After parsing args.address:
let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
let minter_address = state::read_state(|s| runtime.derive_minter_address(s));
if parsed_address == minter_address {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

This makes the guard case-insensitive and format-agnostic, closing the bypass for all bech32 case variants. Apply the same fix to `retrieve_btc_with_approval`.

---

### Proof of Concept

1. Obtain the minter's canonical address string, e.g. `bc1q0jrxz4jh59t5qsu7l0y59kpfdmgjcq60wlee3h`.
2. Convert it to uppercase: `BC1Q0JRXZ4JH59T5QSU7L0Y59KPFDMGJCQ60WLEE3H`.
3. Call `retrieve_btc` with `{ address = "BC1Q0JRXZ4JH59T5QSU7L0Y59KPFDMGJCQ60WLEE3H", amount = <your_balance> }`.
4. Observe: the guard at line 158 does **not** trap (strings differ in case); `BitcoinAddress::parse` succeeds and returns the same `P2wpkhV0` bytes; ckBTC is burned; the withdrawal request is accepted targeting the minter's own address.
5. The minter subsequently broadcasts a Bitcoin transaction to its own address; the user receives no BTC and no reimbursement.

**Relevant code locations:**

Guard (case-sensitive string comparison): [1](#0-0) 

Same guard in `retrieve_btc_with_approval`: [2](#0-1) 

Case-insensitive HRP normalisation in the parser: [3](#0-2) 

Unit test confirming uppercase bech32 parses identically: [4](#0-3) 

`display()` always produces lowercase output (minter address string is always lowercase): [5](#0-4) 

`derive_minter_address_str` (source of `main_address_str`): [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L156-160)
```rust
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L254-258)
```rust
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L49-69)
```rust
    pub fn display(&self, network: Network) -> String {
        match self {
            Self::P2wpkhV0(pkhash) => encode_bech32(network, pkhash, WitnessVersion::V0),
            Self::P2wshV0(pkhash) => encode_bech32(network, pkhash, WitnessVersion::V0),
            Self::P2pkh(pkhash) => version_and_hash_to_address(
                match network {
                    Network::Mainnet => BTC_MAINNET_PREFIX,
                    Network::Testnet | Network::Regtest => BTC_TESTNET_PREFIX,
                },
                pkhash,
            ),
            Self::P2sh(script_hash) => version_and_hash_to_address(
                match network {
                    Network::Mainnet => BTC_MAINNET_P2SH_PREFIX,
                    Network::Testnet | Network::Regtest => BTC_TESTNET_P2SH_PREFIX,
                },
                script_hash,
            ),
            Self::P2trV1(pkhash) => encode_bech32(network, pkhash, WitnessVersion::V1),
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L340-349)
```rust
    let (found_hrp, five_bit_groups, variant) =
        bech32::decode(address).map_err(|e| ParseAddressError::MalformedAddress(e.to_string()))?;
    let expected_hrp = hrp(network);

    if found_hrp.to_lowercase() != expected_hrp {
        return Err(ParseAddressError::UnexpectedHumanReadablePart {
            expected: expected_hrp.to_string(),
            actual: found_hrp,
        });
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L471-480)
```rust
        assert_eq!(
            Ok(BitcoinAddress::P2wpkhV0([
                117, 30, 118, 232, 25, 145, 150, 212, 84, 148, 28, 69, 209, 179, 163, 35, 241, 67,
                59, 214
            ])),
            BitcoinAddress::parse(
                "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
                Network::Mainnet
            )
        );
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1809-1811)
```rust
    fn derive_minter_address_str(&self, state: &CkBtcMinterState) -> String {
        self.derive_minter_address(state).display(state.btc_network)
    }
```
