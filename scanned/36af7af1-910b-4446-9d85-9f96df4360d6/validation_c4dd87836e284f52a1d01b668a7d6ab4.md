### Title
ckBTC Minter Address Self-Send Guard Bypassed via Uppercase Bech32 Address - (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

### Summary

Both `retrieve_btc` and `retrieve_btc_with_approval` in the ckBTC minter guard against a user withdrawing BTC to the minter's own address using a **case-sensitive string comparison**. Because bech32 Bitcoin addresses are case-insensitive by specification, a caller can supply the minter's address in uppercase (or mixed case) to silently bypass this guard. The address then parses successfully to the minter's own `BitcoinAddress`, ckBTC is burned, and BTC is sent back to the minter — the caller receives nothing.

### Finding Description

In `retrieve_btc` (and identically in `retrieve_btc_with_approval`):

```rust
let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

if args.address == main_address_str {          // ← case-sensitive ==
    ic_cdk::trap("illegal retrieve_btc target");
}
// ...
let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
```

`derive_minter_address_str` always produces a **lowercase** bech32 string (via `bech32::encode`, which emits lowercase). [1](#0-0) 

The guard therefore only blocks the exact lowercase string. However, `BitcoinAddress::parse` explicitly accepts **uppercase** bech32 — this is confirmed by the in-tree test: [2](#0-1) 

```rust
assert_eq!(
    Ok(BitcoinAddress::P2wpkhV0([117, 30, ...])),
    BitcoinAddress::parse(
        "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",   // uppercase
        Network::Mainnet
    )
);
```

`parse_bip173_address` normalises the HRP with `.to_lowercase()` and decodes the data portion through the case-insensitive bech32 bit-conversion, so the uppercase and lowercase forms decode to the **identical** `BitcoinAddress::P2wpkhV0(pkhash)`. [3](#0-2) 

The two functions that contain the incomplete guard: [4](#0-3) [5](#0-4) 

This is directly analogous to the reported Lombard pattern: there are **two valid representations** of the minter's address (lowercase and uppercase bech32), but the guard only checks one of them.

### Impact Explanation

When a caller supplies the minter's own address in uppercase:

1. The string guard is not triggered (strings differ).
2. `BitcoinAddress::parse` succeeds and returns `P2wpkhV0(minter_pkhash)`.
3. The caller's ckBTC is **burned** on the ledger.
4. A Bitcoin transaction is constructed and submitted with the minter's own address as the output.
5. The minter later sweeps that UTXO as change via `fetch_main_utxos`, absorbing the BTC into its pool.
6. The caller receives **no BTC** and has permanently lost their ckBTC.

This is a **ledger conservation / chain-fusion burn bug**: ckBTC supply decreases without a corresponding reduction in the minter's BTC holdings, breaking the 1:1 peg invariant for the affected user. The TLA+ spec explicitly models this as a forbidden state (`Prevent_Retrievals_To_Change_Address`), confirming the design intent that no withdrawal should target the minter's address. [6](#0-5) 

### Likelihood Explanation

The minter's Bitcoin address is publicly queryable via `get_btc_address`. Bech32 addresses are routinely displayed in uppercase by QR-code generators, hardware wallets, and block explorers. A user who copies the minter's address from such a source and calls `retrieve_btc` would silently lose funds. An adversarial front-end could also deliberately inject the uppercase form. The entry path requires only a standard unprivileged ingress call to `retrieve_btc` or `retrieve_btc_with_approval`.

### Recommendation

Replace the string comparison with a comparison of the **parsed** `BitcoinAddress` value, which is representation-independent:

```rust
let main_address = state::read_state(|s| runtime.derive_minter_address(s));
let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
if parsed_address == main_address {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

This eliminates the case-sensitivity gap and is consistent with how the rest of the minter compares addresses structurally.

### Proof of Concept

1. Query the minter's address: `dfx canister call ckbtc_minter get_btc_address '(record { owner = opt principal "minter-id"; subaccount = null })'` → returns e.g. `bc1q...xyz`.
2. Convert to uppercase: `BC1Q...XYZ`.
3. Transfer ckBTC to the minter's withdrawal account.
4. Call `retrieve_btc '(record { address = "BC1Q...XYZ"; amount = <amount> })'`.
5. Observe: the call succeeds (no trap), ckBTC is burned on the ledger, and a Bitcoin transaction is submitted sending BTC to the minter's own address. The caller receives no BTC.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1809-1811)
```rust
    fn derive_minter_address_str(&self, state: &CkBtcMinterState) -> String {
        self.derive_minter_address(state).display(state.btc_network)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L336-395)
```rust
fn parse_bip173_address(
    address: &str,
    network: Network,
) -> Result<BitcoinAddress, ParseAddressError> {
    let (found_hrp, five_bit_groups, variant) =
        bech32::decode(address).map_err(|e| ParseAddressError::MalformedAddress(e.to_string()))?;
    let expected_hrp = hrp(network);

    if found_hrp.to_lowercase() != expected_hrp {
        return Err(ParseAddressError::UnexpectedHumanReadablePart {
            expected: expected_hrp.to_string(),
            actual: found_hrp,
        });
    }

    if five_bit_groups.is_empty() {
        return Err(ParseAddressError::NoData);
    }

    let witness_version = five_bit_groups[0].to_u8();

    match witness_version {
        0 => {
            if variant != bech32::Variant::Bech32 {
                return Err(ParseAddressError::InvalidBech32Variant {
                    expected: bech32::Variant::Bech32,
                    found: variant,
                });
            }

            let data = bech32::convert_bits(
                &five_bit_groups[1..],
                /*from=*/ 5,
                /*to=*/ 8,
                /*pad=*/ false,
            )
            .map_err(|e| {
                ParseAddressError::MalformedAddress(format!(
                    "failed to decode witness from address {address}: {e}"
                ))
            })?;

            match data.len() {
                20 => {
                    let mut pkhash = [0_u8; 20];
                    pkhash[..].copy_from_slice(&data[..]);

                    Ok(BitcoinAddress::P2wpkhV0(pkhash))
                }
                32 => {
                    let mut script_hash = [0_u8; 32];
                    script_hash[..].copy_from_slice(&data[..]);

                    Ok(BitcoinAddress::P2wshV0(script_hash))
                }
                _ => Err(ParseAddressError::BadWitnessLength {
                    expected: 20,
                    actual: data.len(),
                }),
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L155-160)
```rust
    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L253-258)
```rust
    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/spec/Ck_BTC.tla (L1415-1428)
```text
Prevent_Retrievals_To_Change_Address ==
    \* The pid constraints determine the moment when the model decides on the destination of
    \* a BTC retrieval...
    \A pid \in RETRIEVE_BTC_PROCESS_IDS:
            /\ pc[pid] = "Retrieve_BTC_Wait_Burn"
            /\ pc'[pid] = "Done"
            /\ pending' # pending
        =>
            \* ...and we require that the destination is not the change address
            Head(pending').address # MINTER_BTC_ADDRESS
 
 Prevent_Donations_To_Change_Address ==
    /\ Prevent_External_Transfers_To_Change_Address
    /\ Prevent_Retrievals_To_Change_Address
```
