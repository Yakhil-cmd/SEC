### Title
P2SH Address Bypass of Self-Address Guard Causes Permanent DOGE Lock — (`rs/dogecoin/ckdoge/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The self-address guard in `retrieve_btc_with_approval` is a **string equality check** against the minter's P2PKH address string. Because the minter's main address is always P2PKH, a P2SH address encoding the **same 20-byte hash** produces a different string, bypasses the guard, passes `parse_address`, and causes the minter to send DOGE to an address it can never spend from.

---

### Finding Description

**Step 1 — The guard is string-only.**

In `retrieve_btc_with_approval`:

```rust
let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));
if args.address == main_address_str {
    ic_cdk::trap("illegal retrieve_btc target");
}
``` [1](#0-0) 

**Step 2 — `derive_minter_address_str` always returns a P2PKH string.**

`derive_minter_address_str` calls `account_to_p2pkh_address_from_state`, which always produces a `DogecoinAddress::P2pkh`. The resulting display string uses version byte `30` (mainnet), producing a `D…` address. [2](#0-1) [3](#0-2) 

**Step 3 — `parse_address` accepts P2SH addresses.**

`DogeCanisterRuntime::parse_address` calls `DogecoinAddress::parse`, which accepts both P2PKH (version byte `30`) and P2SH (version byte `22`) addresses and returns `Ok`. [4](#0-3) [5](#0-4) 

**Step 4 — The attack.**

Let H = the 20-byte hash embedded in the minter's P2PKH address (i.e., `hash160(minter_pubkey)`). An attacker:

1. Calls `get_doge_address` to obtain the minter's P2PKH address string.
2. Base58check-decodes it to extract H.
3. Constructs a P2SH address: `base58check(22 ‖ H)` — a different string, same hash bytes.
4. Calls `retrieve_doge_with_approval({amount, address: P2SH_string, from_subaccount: None})`.

The string equality check `args.address == main_address_str` compares the P2SH string to the P2PKH string — they differ in the version byte, so the guard **does not fire**. `parse_address` succeeds, returning `BitcoinAddress::P2sh(H)`. The minter records and processes the withdrawal, sending DOGE to the P2SH address.

**Step 5 — The minter cannot spend from the P2SH address.**

The minter's transaction signer (`DogecoinTransactionSigner`) only constructs P2PKH `script_sig`s. It has no P2SH redeem-script logic. [6](#0-5) 

Furthermore, the minter only monitors UTXOs at its P2PKH address string. UTXOs at the P2SH address are never fetched, never tracked, and never spendable. The DOGE is permanently locked.

---

### Impact Explanation

Each successful call burns the attacker's ckDOGE (via `icrc2_transfer_from`) and causes the minter to irreversibly send real DOGE to an unspendable P2SH address. Repeated calls drain the minter's DOGE reserves. The locked DOGE is unrecoverable without a canister upgrade that adds P2SH spending logic and knowledge of the specific redeem script.

---

### Likelihood Explanation

The attack requires no privilege — only a non-anonymous principal with a ckDOGE balance and an ICRC-2 approval. The minter's P2PKH address is publicly queryable. Constructing the P2SH address from it is trivial base58check arithmetic. There is no address-type check anywhere in the withdrawal path that would reject a P2SH address. The ckDOGE minter also has **no OFAC/taint check** for withdrawal addresses (the `check_address` implementation unconditionally returns `Clean`), removing the only other potential filter. [7](#0-6) 

---

### Recommendation

Replace the string equality guard with a **semantic** comparison. After parsing the user-supplied address, compare the resulting `BitcoinAddress` variant and hash against `derive_minter_address(state)`:

```rust
let main_address = state::read_state(|s| runtime.derive_minter_address(s));
let parsed_address = runtime.parse_address(&args.address, btc_network)?;
if parsed_address == main_address {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

This catches any address type (P2PKH, P2SH, or future types) that encodes the same underlying hash as the minter's main address.

---

### Proof of Concept

```rust
// 1. Obtain minter's P2PKH address (e.g. "D8vFiz4...")
let p2pkh_str = minter.get_doge_address(...);

// 2. Decode to extract the 20-byte hash H
let decoded = bs58::decode(&p2pkh_str).into_vec().unwrap();
// decoded = [30, H[0..20], checksum[0..4]]
let hash: [u8; 20] = decoded[1..21].try_into().unwrap();

// 3. Re-encode as P2SH (version byte 22 for mainnet)
let mut buf = vec![22u8];
buf.extend_from_slice(&hash);
let sha256d = sha256(&sha256(&buf));
buf.extend_from_slice(&sha256d[0..4]);
let p2sh_str = bs58::encode(&buf).into_string();
// p2sh_str != p2pkh_str  →  guard does not fire

// 4. Submit withdrawal
minter.retrieve_doge_with_approval(RetrieveDogeWithApprovalArgs {
    amount: withdrawal_amount,
    address: p2sh_str,   // accepted by parse_address, bypasses guard
    from_subaccount: None,
});
// Minter sends DOGE to P2SH address → permanently locked
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L253-258)
```rust
    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/lib.rs (L186-201)
```rust
    fn parse_address(
        &self,
        address: &str,
        network: ic_ckbtc_minter::Network,
    ) -> Result<BitcoinAddress, std::string::String> {
        let doge_network = Network::try_from(network)?;
        let doge_address =
            DogecoinAddress::parse(address, &doge_network).map_err(|e| e.to_string())?;

        // This conversion is a hack to use the same type of address as in RetrieveBtcRequest,
        // since this type is used both in the event logs (event `AcceptedRetrieveBtcRequest`)
        // and in the minter state (field `pending_retrieve_btc_requests`)
        Ok(match doge_address {
            DogecoinAddress::P2pkh(bytes) => BitcoinAddress::P2pkh(bytes),
            DogecoinAddress::P2sh(bytes) => BitcoinAddress::P2sh(bytes),
        })
```

**File:** rs/dogecoin/ckdoge/minter/src/lib.rs (L223-231)
```rust
    fn derive_minter_address_str(&self, state: &CkBtcMinterState) -> String {
        let main_account = Account {
            owner: ic_cdk::api::canister_self(),
            subaccount: None,
        };
        let minter_address = updates::account_to_p2pkh_address_from_state(state, &main_account);
        minter_address
            .display(&Network::try_from(state.btc_network).expect("BUG: unsupported network"))
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/lib.rs (L233-240)
```rust
    async fn check_address(
        &self,
        _btc_checker_principal: Option<Principal>,
        _address: String,
    ) -> Result<BtcAddressCheckStatus, CallError> {
        // No OFAC checklist for Dogecoin addresses
        Ok(BtcAddressCheckStatus::Clean)
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L38-40)
```rust
pub fn account_to_p2pkh_address(public_key: &ECDSAPublicKey, account: &Account) -> DogecoinAddress {
    DogecoinAddress::p2pkh_from_public_key(&derive_public_key(public_key, account))
}
```

**File:** rs/dogecoin/ckdoge/minter/src/address/mod.rs (L89-106)
```rust
        match (bytes[0], network) {
            (DOGE_MAINNET_P2PKH_PREFIX, Network::Mainnet)
            | (DOGE_REGTEST_P2PKH_PREFIX, Network::Regtest) => Ok(Self::P2pkh(data)),
            (DOGE_MAINNET_P2SH_PREFIX, Network::Mainnet)
            | (DOGE_REGTEST_P2SH_PREFIX, Network::Regtest) => Ok(Self::P2sh(data)),
            (DOGE_MAINNET_P2PKH_PREFIX, _) | (DOGE_MAINNET_P2SH_PREFIX, _) => {
                Err(ParseAddressError::WrongNetwork {
                    actual: Network::Mainnet,
                    expected: *network,
                })
            }
            (DOGE_REGTEST_P2PKH_PREFIX, _) => Err(ParseAddressError::WrongNetwork {
                actual: Network::Regtest,
                expected: *network,
            }),
            _ => Err(ParseAddressError::UnsupportedAddressType),
        }
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/transaction/mod.rs (L59-68)
```rust
            let address = DogecoinAddress::p2pkh_from_public_key(&public_key);
            assert!(
                matches!(address, DogecoinAddress::P2pkh(_)),
                "BUG: expected P2PKH address. Other type of addresses would require other script_sig."
            );
            let script_pubkey = bitcoin::ScriptBuf::new_p2pkh(
                &bitcoin::PubkeyHash::from_byte_array(address.into_array()),
            );
            let sighash = cache
                .legacy_signature_hash(input_index, &script_pubkey, sighash_type.to_u32())
```
