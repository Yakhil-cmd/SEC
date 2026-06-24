Based on my analysis of the code, here is my finding:

---

### Title
Forked-chain permanent injection via store-before-validate ordering in `sync_blocks_interval` — (`rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs`)

### Summary

A malicious ICRC-1 ledger canister operator can permanently inject a hash-chain-disconnected batch of forged blocks into Rosetta's SQLite database. The root cause is that `sync_blocks_interval` stores each fetched batch **before** performing the trailing parent-hash check that connects the batch to the already-stored chain. Because `store_blocks` uses `INSERT OR IGNORE`, the forged blocks can never be overwritten by subsequent correct syncs.

---

### Finding Description

**Step 1 — Certified tip is attacker-controlled.**

`get_certified_chain_tip()` in `packages/icrc-ledger-agent/src/lib.rs` correctly verifies the IC BLS certificate: [1](#0-0) 

However, the IC certificate is signed by the subnet over whatever `certified_data` the canister itself sets via `ic0.certified_data_set()`. A canister that the attacker controls can set `certified_data` to the digest of a hash-tree containing any `tip_hash` / `last_block_hash` value. The subnet will faithfully sign a valid certificate over it. So `get_certified_chain_tip()` returns an attacker-chosen hash as `tip_block_hash`.

**Step 2 — `sync_from_the_tip` anchors the sync range to the attacker's hash.** [2](#0-1) 

`leading_block_hash` = attacker's forged tip hash; `trailing_parent_hash` = hash of the last legitimately stored block (read from the local DB, not from the ledger).

**Step 3 — `sync_blocks_interval` stores blocks BEFORE the trailing-hash check.**

Inside the loop:

1. Fetch batch (line 403)
2. `is_valid_blockchain` — validates internal chain consistency **and** that the highest block's hash equals `leading_block_hash` (line 419). This passes because the attacker crafted the chain to end at their certified hash.
3. **Store the batch** (line 444) — blocks are written to SQLite here.
4. Update `leading_block_hash` to the parent hash of the lowest fetched block (line 440).
5. On the final iteration only: check `leading_block_hash == sync_range.trailing_parent_hash` (line 463). [3](#0-2) 

If the forged chain's lowest block does not connect to the real stored chain, step 5 fires `bail!` — but the forged blocks are **already committed** to the database.

**Step 4 — `INSERT OR IGNORE` makes the injection permanent.** [4](#0-3) 

On every subsequent sync cycle, `get_block_with_highest_block_idx()` returns the forged tip, so `trailing_parent_hash` is now the forged block's hash. The real ledger's blocks at those indices are silently dropped by `INSERT OR IGNORE`. The database is permanently on the forged fork.

---

### Impact Explanation

After injection, `update_account_balances` processes the forged blocks and writes fabricated balances to the `account_balances` table. Any downstream exchange or custodian querying Rosetta for balances receives phantom token amounts derived from the forged chain. The impact is proportional to the token amounts the attacker mints in the forged blocks — there is no on-chain cap enforced by Rosetta's local DB.

---

### Likelihood Explanation

The attacker must control the ICRC-1 ledger canister that a Rosetta instance is configured to sync from. On the IC, deploying a canister is permissionless. The realistic threat model is a malicious token issuer whose token an exchange has listed and whose Rosetta instance is pointed at the issuer's canister. This is a concrete, non-hypothetical deployment pattern.

---

### Recommendation

Move the trailing parent-hash check **before** calling `storage_client.store_blocks(...)`. Specifically, after computing `leading_block_hash = fetched_blocks[0].get_parent_hash()` (line 440), and when `next_index_interval.start() == sync_range.index_range.start()`, verify `leading_block_hash == sync_range.trailing_parent_hash` **before** the `store_blocks` call. Only store the batch if the check passes. This eliminates the window in which forged blocks can be committed to the DB.

Alternatively, wrap the entire `sync_blocks_interval` in a DB transaction that is rolled back on any validation failure, including the trailing-hash check.

---

### Proof of Concept

```
1. Sync N real blocks into Rosetta (indices 0..N-1).
2. Replace the Icrc1Agent with a mock that:
   a. Returns a certified tip at index N+M-1 with hash H_fake
      (set certified_data = hash_tree{tip_hash: H_fake} on the canister).
   b. Returns M forged blocks [N..N+M-1] forming a valid internal chain
      ending at H_fake, but with block[N].parent_hash ≠ hash(real block N-1).
3. Call sync_blocks_interval / sync_from_the_tip.
4. is_valid_blockchain passes (internal consistency + leading hash == H_fake).
5. store_blocks commits forged blocks N..N+M-1 to SQLite.
6. Trailing-hash check fires bail! — but DB already contains forged blocks.
7. Call update_account_balances.
8. Assert fabricated balances appear in account_balances table.
9. Re-run sync with the real ledger; assert real blocks at N..N+M-1
   are silently dropped by INSERT OR IGNORE.
```

### Citations

**File:** packages/icrc-ledger-agent/src/lib.rs (L342-343)
```rust
        self.verify_root_hash(&certificate, &hash_tree.digest())
            .await?;
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L285-299)
```rust
    let sync_range = storage_client
        .get_block_with_highest_block_idx()
        .await?
        .map_or(
            SyncRange::new(0, tip_block_index, ByteBuf::from(tip_block_hash), None),
            |block| {
                SyncRange::new(
                    // If storage is up to date then the start index is the same as the tip of the ledger.
                    block.index + 1,
                    tip_block_index,
                    ByteBuf::from(tip_block_hash),
                    Some(block.clone().get_block_hash()),
                )
            },
        );
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L419-471)
```rust
        if let Err(error) = blocks_verifier::is_valid_blockchain(
            &fetched_blocks,
            &leading_block_hash.clone().unwrap(),
        ) {
            // Abort synchronization if blockchain is not valid.
            bail!(
                "The fetched blockchain contains invalid blocks in index range {} to {}: {error}",
                next_index_interval.start(),
                next_index_interval.end()
            );
        }

        // Verify that the indices that are returned by the replica match those that were requested (Block Indices are not part of the block hash)
        if !blocks_verifier::indices_are_valid(&fetched_blocks, next_index_interval.clone()) {
            bail!(
                "The fetched blockchain is not a left bound subset of the requested indices in index range {} to {}",
                next_index_interval.start(),
                next_index_interval.end()
            );
        }

        leading_block_hash.clone_from(&fetched_blocks[0].get_parent_hash());
        let number_of_blocks_fetched = fetched_blocks.len() as u64;

        // Store the fetched blocks in the database.
        let result = storage_client.store_blocks(fetched_blocks.clone()).await;
        if let Err(e) = result {
            error!("Error while calling storage_client.store_blocks: {}", e);
            return Err(e);
        }
        storage_client
            .get_metrics()
            .add_blocks_fetched(number_of_blocks_fetched);
        // The first iteration of the loop will fetch blocks up to the end of the `sync_range`.
        // Subsequent iterations will fetch blocks with lower indexes, and calls to
        // `set_synced_height` will be redundant but harmless.
        storage_client
            .get_metrics()
            .set_synced_height(*sync_range.index_range.end());
        pr.update(number_of_blocks_fetched);

        // If the interval of the last iteration started at the target height, then all blocks above and including the target height have been synched.
        if *next_index_interval.start() == *sync_range.index_range.start() {
            // All blocks were fetched, now the parent hash of the lowest block fetched has to match the hash of the highest block in the database or `None` (If database was empty).
            if leading_block_hash == sync_range.trailing_parent_hash {
                break;
            } else {
                bail!(
                    "Hash of block {} in database does not match parent hash of fetched block {}",
                    next_index_interval.start().saturating_sub(1),
                    next_index_interval.start()
                )
            }
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L686-710)
```rust
        insert_tx.prepare_cached(
        "INSERT OR IGNORE INTO blocks (idx, hash, serialized_block, parent_hash, timestamp,tx_hash,operation_type,from_principal,from_subaccount,to_principal,to_subaccount,spender_principal,spender_subaccount,memo,amount,expected_allowance,fee,transaction_created_at_time,approval_expires_at) VALUES (:idx, :hash, :serialized_block, :parent_hash, :timestamp,:tx_hash,:operation_type,:from_principal,:from_subaccount,:to_principal,:to_subaccount,:spender_principal,:spender_subaccount,:memo,:amount,:expected_allowance,:fee,:transaction_created_at_time,:approval_expires_at)")?
                    .execute(named_params! {
                        ":idx":rosetta_block.index,
                        ":hash":rosetta_block.clone().get_block_hash().as_slice().to_vec(),
                        ":serialized_block":rosetta_block.block,
                        ":parent_hash":rosetta_block.get_parent_hash().clone().map(|hash| hash.as_slice().to_vec()),
                        ":timestamp":timestamp_i64,
                        ":tx_hash":rosetta_block.clone().get_transaction_hash().as_slice().to_vec(),
                        ":operation_type":operation_type,
                        ":from_principal":from_principal.map(|x| x.as_slice().to_vec()),
                        ":from_subaccount":from_subaccount,
                        ":to_principal":to_principal.map(|x| x.as_slice().to_vec()),
                        ":to_subaccount":to_subaccount,
                        ":spender_principal":spender_principal.map(|x| x.as_slice().to_vec()),
                        ":spender_subaccount":spender_subaccount,
                        ":memo":transaction.memo.map(|x| x.0.as_slice().to_vec()),
                        ":amount":amount.to_string(),
                        ":expected_allowance":expected_allowance.map(|ea| ea.to_string()),
                        ":fee":fee.map(|fee| fee.to_string()),
                        ":transaction_created_at_time":transaction_created_at_time_i64,
                        ":approval_expires_at":approval_expires_at_i64
                    })?;
    }
    insert_tx.commit()?;
```
