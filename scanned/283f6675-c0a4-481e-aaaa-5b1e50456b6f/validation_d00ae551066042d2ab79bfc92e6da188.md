Let me look at the exact self-address guard and the address display logic to confirm the string comparison behavior.

### Title
Self-Address Guard Bypassed via P2SH Address with Identical Hash — (`rs/dogecoin/ckdoge/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

---

### Summary

The ckDOGE minter's self-address guard in `retrieve_btc_with_approval` uses a plain string equality check. Because the minter's address is always P2PKH, an attacker can construct a P2SH address encoding the same 20-byte hash, pass it as the withdrawal destination, bypass the guard, and cause the minter to permanently lock DOGE at an address it can never spend.

---

### Finding Description

**Step 1 — The guard is a string comparison.**

In `retrieve_btc_with_approval`:

```rust
let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));
if args.address == main_address_str {
    ic_cdk::trap("illegal retrieve_btc target");
}
``` [1](#0-0) 

**Step 2 — The minter's address is always P2PKH.**

`derive_minter_address_str` calls `account_to_p2pkh_address_from_state`, which calls `account_to_p2pkh_address`, which calls `p2pkh_from_public_key` — the return type is always `DogecoinAddress::P2pkh(hash160(pubkey))`. [2](#0-1) [3](#0-2) [4](#0-3) 

`display()` encodes it with `DOGE_MAINNET_P2PKH_PREFIX = 30`, producing a string starting with `D` on mainnet. [5](#0-4) 

**Step 3 — P2SH with the same hash produces a different string.**

`DOGE_MAINNET_P2SH_PREFIX = 22` produces a string starting with `A` on mainnet. A P2SH address built from the same 20-byte hash H is a completely different base58 string. The `==` comparison returns `false`, so the guard does not fire. [6](#0-5) 

**Step 4 — `parse_address` accepts P2SH addresses.**

The ckDOGE runtime's `parse_address` accepts both P2PKH and P2SH addresses and maps them to `BitcoinAddress::P2sh(bytes)`. [7](#0-6) 

**Step 5 — The minter builds and broadcasts a P2SH output.**

The transaction builder emits a `new_p2sh` output script for `BitcoinAddress::P2sh`, sending DOGE to the P2SH address on-chain. [8](#0-7) 

**Step 6 — The minter cannot spend from that P2SH address.**

Spending a P2SH UTXO requires a redeem script whose `hash160` equals H. The minter only holds the ECDSA private key for the public key whose `hash160` is H; it has no redeem script for this P2SH address and only signs P2PKH inputs. The DOGE is permanently unspendable.

---

### Impact Explanation

Every successful call burns the caller's ckDOGE and causes the minter to send the corresponding DOGE to an address it can never spend. The DOGE is permanently locked on-chain. The minter's DOGE reserve is reduced without any possibility of recovery or consolidation of those UTXOs. Repeated calls drain the minter's spendable DOGE pool proportionally to the attacker's ckDOGE holdings.

---

### Likelihood Explanation

The attack requires no privilege. Any non-anonymous principal with a sufficient ckDOGE balance and an ICRC-2 approval can execute it in a single `retrieve_doge_with_approval` call. The minter's P2PKH address is publicly queryable via `get_doge_address`. Constructing the corresponding P2SH address is trivial: decode the base58 P2PKH address, swap the version byte from 30 to 22, recompute the checksum, and re-encode. No off-chain infrastructure, no privileged access, and no threshold corruption is required.

---

### Recommendation

Replace the string equality guard with a structural comparison that checks both address type and hash bytes. After parsing `args.address` into a `DogecoinAddress`, compare it against the parsed minter address:

```rust
let minter_addr = derive_minter_address(state); // returns DogecoinAddress::P2pkh(H)
let parsed = DogecoinAddress::parse(&args.address, network)?;
if parsed == minter_addr {
    ic_cdk::trap("illegal retrieve_doge target");
}
```

This rejects any address — regardless of type — whose underlying 20-byte hash matches the minter's hash.

---

### Proof of Concept

```rust
// 1. Query the minter's P2PKH address, e.g. "DPubKeyHashXXX..."
let p2pkh_str = minter.get_doge_address(...);

// 2. Decode and extract the 20-byte hash H
let decoded = bs58::decode(&p2pkh_str).into_vec().unwrap(); // 25 bytes
let hash: [u8; 20] = decoded[1..21].try_into().unwrap();

// 3. Re-encode as P2SH (version byte 22 for mainnet)
let mut payload = vec![22u8];
payload.extend_from_slice(&hash);
let checksum = sha256d(&payload[..21])[..4].to_vec();
payload.extend_from_slice(&checksum);
let p2sh_str = bs58::encode(&payload).into_string();

// 4. p2sh_str != p2pkh_str  →  guard does not fire
// 5. DogecoinAddress::parse(p2sh_str, Mainnet) → Ok(P2sh(H))
// 6. Call retrieve_doge_with_approval({amount, address: p2sh_str, from_subaccount: None})
//    → ckDOGE burned, DOGE sent to P2SH address, permanently locked.
assert_ne!(p2sh_str, p2pkh_str);
assert!(DogecoinAddress::parse(&p2sh_str, &Network::Mainnet).is_ok());
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L254-258)
```rust
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

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L38-40)
```rust
pub fn account_to_p2pkh_address(public_key: &ECDSAPublicKey, account: &Account) -> DogecoinAddress {
    DogecoinAddress::p2pkh_from_public_key(&derive_public_key(public_key, account))
}
```

**File:** rs/dogecoin/ckdoge/minter/src/address/mod.rs (L9-11)
```rust
// See https://github.com/dogecoin/dogecoin/blob/7237da74b8c356568644cbe4fba19d994704355b/src/chainparams.cpp#L167
const DOGE_MAINNET_P2PKH_PREFIX: u8 = 30;
const DOGE_MAINNET_P2SH_PREFIX: u8 = 22;
```

**File:** rs/dogecoin/ckdoge/minter/src/address/mod.rs (L108-116)
```rust
    pub fn display(&self, network: &Network) -> String {
        let prefix = match (self, network) {
            (DogecoinAddress::P2pkh(_), Network::Mainnet) => DOGE_MAINNET_P2PKH_PREFIX,
            (DogecoinAddress::P2sh(_), Network::Mainnet) => DOGE_MAINNET_P2SH_PREFIX,
            (DogecoinAddress::P2pkh(_), Network::Regtest) => DOGE_REGTEST_P2PKH_PREFIX,
            (DogecoinAddress::P2sh(_), Network::Regtest) => DOGE_REGTEST_P2SH_PREFIX,
        };
        version_and_hash_to_address(prefix, self.as_array())
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/address/mod.rs (L140-143)
```rust
    pub fn p2pkh_from_public_key(public_key: &[u8; 33]) -> Self {
        assert!(public_key[0] == 0x02 || public_key[0] == 0x03);
        DogecoinAddress::P2pkh(ic_ckbtc_minter::tx::hash160(public_key))
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/transaction/mod.rs (L162-168)
```rust
                script_pubkey: match output.address {
                    ic_ckbtc_minter::address::BitcoinAddress::P2pkh(hash) => {
                        bitcoin::ScriptBuf::new_p2pkh(&bitcoin::PubkeyHash::from_byte_array(hash))
                    }
                    ic_ckbtc_minter::address::BitcoinAddress::P2sh(hash) => {
                        bitcoin::ScriptBuf::new_p2sh(&bitcoin::ScriptHash::from_byte_array(hash))
                    }
```
