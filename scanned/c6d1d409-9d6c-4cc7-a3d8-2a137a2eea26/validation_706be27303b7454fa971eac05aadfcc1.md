### Title
Case-Insensitive Bech32 Parsing Bypasses Minter Self-Address Guard in `retrieve_btc` — (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

---

### Summary

The ckBTC minter's `retrieve_btc` and `retrieve_btc_with_approval` withdrawal endpoints guard against sending BTC to the minter's own address using a **case-sensitive string comparison**. However, `BitcoinAddress::parse` accepts uppercase bech32 addresses (e.g., `BC1Q…`). An unprivileged caller can supply the minter's address in uppercase, bypass the guard, have their ckBTC burned, and have BTC sent to the minter's own UTXO pool — with no ckBTC minted in return. The user loses their funds.

---

### Finding Description

**Root cause — case-sensitive guard vs. case-insensitive parser:**

In `retrieve_btc`, the self-address guard is:

```rust
if args.address == main_address_str {
    ic_cdk::trap("illegal retrieve_btc target");
}
``` [1](#0-0) 

`main_address_str` is produced by `derive_minter_address_str`, which calls `display()` → `encode_bech32()`, always yielding **lowercase** bech32 (e.g., `bc1q…`). [2](#0-1) 

Immediately after the guard, the address is parsed:

```rust
let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
``` [3](#0-2) 

`BitcoinAddress::parse` explicitly accepts both lowercase and uppercase bech32 by dispatching on the first character `'b'`, `'B'`, `'t'`, `'T'`: [4](#0-3) 

Inside `parse_bip173_address`, the HRP comparison is case-folded:

```rust
if found_hrp.to_lowercase() != expected_hrp {
``` [5](#0-4) 

The same guard and the same parser are present in `retrieve_btc_with_approval`: [6](#0-5) [7](#0-6) 

**The BTC checker does not save the situation.** The BTC checker's `check_address` re-parses the address with the `bitcoin` crate and checks it against the OFAC blocklist. The minter's own address is not on the blocklist, so the check returns `Passed`. [8](#0-7) 

After the BTC checker passes, ckBTC is burned and the `RetrieveBtcRequest` is queued with the parsed (minter-equivalent) address: [9](#0-8) 

---

### Impact Explanation

When BTC is sent to the minter's own address, the minter treats the arriving UTXO as part of its own UTXO pool (used to fund future withdrawals). The minter does **not** mint new ckBTC for UTXOs arriving at its own address — only UTXOs at per-user deposit addresses trigger minting. The caller's ckBTC is irreversibly burned, and the BTC is absorbed into the minter's reserves. The caller receives nothing in return. This is a direct, permanent loss of user funds.

---

### Likelihood Explanation

The minter's address is publicly queryable via `minter_address()`. A user who copies the address from a display that renders it in uppercase (common in some UIs for readability), or who is socially engineered into using the uppercase form, will silently bypass the guard. The attack requires no privileged access, no key material, and no consensus-level corruption — only a standard ingress call to `retrieve_btc` or `retrieve_btc_with_approval` with the minter's address uppercased.

---

### Recommendation

Replace the string-equality guard with a **semantic address comparison** using the already-parsed representation. Parse the minter's address once and compare `BitcoinAddress` values, not strings:

```rust
// After parsing args.address:
let minter_address = runtime.derive_minter_address(state);
if parsed_address == minter_address {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

This eliminates the case-sensitivity gap entirely, regardless of how the caller encodes the address string.

---

### Proof of Concept

1. Query the minter's canonical address, e.g. `bc1qminter…` (lowercase bech32).
2. Convert to uppercase: `BC1QMINTER…`.
3. Call `retrieve_btc(RetrieveBtcArgs { address: "BC1QMINTER…", amount: X })` as any unprivileged principal with sufficient ckBTC balance.
4. **Guard check** (`args.address == main_address_str`): `"BC1QMINTER…" != "bc1qminter…"` → guard does **not** trap.
5. **Parser** (`BitcoinAddress::parse`): accepts uppercase bech32, returns the same `BitcoinAddress::P2wpkhV0([…])` as the minter's address.
6. **BTC checker**: minter's address is not on the OFAC blocklist → `Passed`.
7. ckBTC is burned from the caller's account.
8. `RetrieveBtcRequest` is queued with `address = parsed_address` (the minter's own address).
9. The minter's processing loop sends BTC to its own address; the UTXO is absorbed into the minter's pool. No ckBTC is minted. The caller's funds are permanently lost.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L158-160)
```rust
    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L173-173)
```rust
    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L204-214)
```rust
    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L256-258)
```rust
    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L271-273)
```rust
    let parsed_address = runtime
        .parse_address(&args.address, btc_network)
        .map_err(RetrieveBtcWithApprovalError::MalformedAddress)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1809-1811)
```rust
    fn derive_minter_address_str(&self, state: &CkBtcMinterState) -> String {
        self.derive_minter_address(state).display(state.btc_network)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L80-83)
```rust
            Some('b') => parse_bip173_address(address, network),
            Some('B') => parse_bip173_address(address, network),
            Some('t') => parse_bip173_address(address, network),
            Some('T') => parse_bip173_address(address, network),
```

**File:** rs/bitcoin/ckbtc/minter/src/address.rs (L344-344)
```rust
    if found_hrp.to_lowercase() != expected_hrp {
```

**File:** rs/bitcoin/checker/src/main.rs (L86-103)
```rust
fn check_address(args: CheckAddressArgs) -> CheckAddressResponse {
    let config = get_config();
    let btc_network = config.btc_network();
    let address = Address::from_str(args.address.trim())
        .unwrap_or_else(|err| ic_cdk::trap(format!("Invalid Bitcoin address: {err}")))
        .require_network(btc_network.clone().into())
        .unwrap_or_else(|err| ic_cdk::trap(format!("Not a Bitcoin {btc_network} address: {err}")));

    match config.check_mode {
        CheckMode::AcceptAll => CheckAddressResponse::Passed,
        CheckMode::RejectAll => CheckAddressResponse::Failed,
        CheckMode::Normal => {
            if is_blocked(&address) {
                return CheckAddressResponse::Failed;
            }
            CheckAddressResponse::Passed
        }
    }
```
