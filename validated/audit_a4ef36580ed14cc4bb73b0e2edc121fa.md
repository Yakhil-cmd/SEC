### Title
ckBTC Minter `tokens_minted` Counter Overcounts Actual Minted Supply by `check_fee` Per UTXO - (File: rs/bitcoin/ckbtc/minter/src/state.rs)

### Summary
The ckBTC minter's internal `tokens_minted` accounting counter is incremented by the raw UTXO value (`utxo.value`) on every successful mint, but the actual amount minted to the user on the ckBTC ledger is `utxo.value - check_fee`. This causes `tokens_minted` to permanently overstate the actual ckBTC total supply by `n × check_fee`, where `n` is the number of UTXOs ever processed. The discrepancy is publicly observable via the `ckbtc_minter_minted_tokens` metric and grows monotonically with every deposit.

### Finding Description
In `rs/bitcoin/ckbtc/minter/src/state.rs`, the `add_utxos` method increments `tokens_minted` using the full raw UTXO value:

```rust
self.tokens_minted += utxos.iter().map(|u| u.value).sum::<u64>();
``` [1](#0-0) 

However, in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, the actual amount minted to the user on the ckBTC ledger is `utxo.value - check_fee`:

```rust
let amount = utxo.value - check_fee;
// ...
runtime.mint_ckbtc(amount, caller_account, ...).await
``` [2](#0-1) 

`add_utxos` is called only after a successful `mint_ckbtc` call, passing the original `utxo` struct (whose `.value` is the full BTC satoshi amount, not the net minted amount): [3](#0-2) 

The `check_fee` is deducted from the minted amount but is **not** deducted from the `tokens_minted` counter. The `tokens_burned` field tracks the BTC-side burn (retrieve_btc), while `tokens_minted` is supposed to track the ckBTC-side mint. The two diverge by exactly `n × check_fee`.

The `tokens_minted` state field is defined in `CkBtcMinterState`: [4](#0-3) 

And is exposed publicly as the `ckbtc_minter_minted_tokens` metric: [5](#0-4) 

### Impact Explanation
The `tokens_minted` counter is the minter's authoritative record of total ckBTC ever issued. It is exposed as a public metric endpoint. Any external auditor, monitoring system, or protocol participant querying `ckbtc_minter_minted_tokens` will observe a value that is inflated relative to the actual ckBTC ledger total supply. The invariant `tokens_minted - tokens_burned == ckBTC_total_supply` is violated. The TLA+ model for ckBTC explicitly states the conservation invariant `Inv_No_Unbacked_Ck_BTC`: [6](#0-5) 

The minter's own internal accounting breaks this invariant from the minter's perspective. The discrepancy is `n × check_fee` (currently `check_fee` defaults to a non-zero value), growing permanently with every deposit. This misleads any system relying on `tokens_minted` for conservation checks, auditing, or cross-chain accounting.

### Likelihood Explanation
Every successful `update_balance` call that processes at least one UTXO triggers the discrepancy. This is the normal deposit flow for any ckBTC user. No special privileges are required — any unprivileged principal can call `update_balance` with their own BTC address. The discrepancy accumulates continuously on mainnet with every deposit.

### Recommendation
Change `add_utxos` to accept the net minted amount as a separate parameter, or compute it as `utxo.value - check_fee` before adding to `tokens_minted`. Alternatively, pass the already-computed `amount` (from `update_balance.rs` line 320) into the audit call so the counter reflects what was actually credited on the ledger:

```rust
// In add_utxos, track net minted amount, not raw UTXO value:
self.tokens_minted += net_minted_amount; // passed in from caller
```

The `audit::add_utxos` wrapper should be updated to accept and forward the net minted amount.

### Proof of Concept
1. User deposits a BTC UTXO with `value = 100_000 satoshis`; `check_fee = 2_000 satoshis`.
2. `update_balance` mints `100_000 - 2_000 = 98_000` ckBTC satoshis to the user on the ledger.
3. On success, `audit::add_utxos` is called with the original `utxo` (value = 100_000).
4. `state.add_utxos` executes: `self.tokens_minted += 100_000`.
5. Actual ckBTC ledger total supply increases by `98_000`.
6. After 1,000 such deposits: `tokens_minted = 100_000_000`, actual ledger supply = `98_000_000`. Discrepancy = `2_000_000` satoshis (0.02 BTC) — permanently overcounted and publicly observable via `ckbtc_minter_minted_tokens`. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L503-507)
```rust
    /// The total amount of ckBTC minted.
    pub tokens_minted: u64,

    /// The total amount of ckBTC burned.
    pub tokens_burned: u64,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L776-796)
```rust
    pub(crate) fn add_utxos<I: CheckInvariants>(&mut self, account: Account, utxos: Vec<Utxo>) {
        if utxos.is_empty() {
            return;
        }

        self.tokens_minted += utxos.iter().map(|u| u.value).sum::<u64>();

        let account_bucket = self.utxos_state_addresses.entry(account).or_default();

        for utxo in utxos {
            self.minted_outpoints.insert(utxo.outpoint.clone());
            self.outpoint_account.insert(utxo.outpoint.clone(), account);
            self.available_utxos.insert(utxo.clone());
            self.checked_utxos.remove(&utxo);
            account_bucket.insert(utxo);
        }

        if cfg!(debug_assertions) {
            I::check_invariants(self).expect("state invariants are violated");
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L318-358)
```rust
        };

        let amount = utxo.value - check_fee;
        let memo = MintMemo::Convert {
            txid: Some(utxo.outpoint.txid.as_ref()),
            vout: Some(utxo.outpoint.vout),
            kyt_fee: Some(check_fee),
        };

        // After the call to `mint_ckbtc` returns, in a very unlikely situation the
        // execution may panic/trap without persisting state changes and then we will
        // have no idea whether the mint actually succeeded or not. If this happens
        // the use of the guard below will help set the utxo to `CleanButMintUnknown`
        // status so that it will not be minted again. Utxos with this status will
        // require manual intervention.
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });

        match runtime
            .mint_ckbtc(amount, caller_account, crate::memo::encode(&memo).into())
            .await
        {
            Ok(block_index) => {
                log!(
                    Priority::Debug,
                    "Minted {amount} {token_name} for account {caller_account} corresponding to utxo {} with value {}",
                    DisplayOutpoint(&utxo.outpoint),
                    DisplayAmount(utxo.value),
                );
                state::mutate_state(|s| {
                    state::audit::add_utxos(
                        s,
                        Some(block_index),
                        caller_account,
                        vec![utxo.clone()],
                        runtime,
                    )
                });
```

**File:** rs/bitcoin/ckbtc/minter/src/metrics.rs (L255-265)
```rust
    metrics.encode_counter(
        "ckbtc_minter_minted_tokens",
        state::read_state(|s| s.tokens_minted) as f64,
        "Total number of minted tokens.",
    )?;

    metrics.encode_counter(
        "ckbtc_minter_burned_tokens",
        state::read_state(|s| s.tokens_burned) as f64,
        "Total number of burned tokens.",
    )?;
```

**File:** rs/bitcoin/ckbtc/spec/Ck_BTC.tla (L1295-1298)
```text
\* The main invariant: the sum of balances on the CkBTC ledger never exceeds the sum of UTXOs
\* controlled by minter addresses
Inv_No_Unbacked_Ck_BTC == 
    Sum_F(LAMBDA x: balance[x], DOMAIN balance) <= BTC_Balance_Of(CK_BTC_ADDRESSES \union {MINTER_BTC_ADDRESS})
```
