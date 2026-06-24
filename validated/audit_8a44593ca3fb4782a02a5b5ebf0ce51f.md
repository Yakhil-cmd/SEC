Audit Report

## Title
Store-before-validate ordering allows permanent forged-block injection into Rosetta SQLite database — (`rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs`)

## Summary

In `sync_blocks_interval`, each fetched batch is committed to the SQLite database via `store_blocks` (line 444) before the trailing parent-hash check that anchors the batch to the previously stored chain (line 463). Because the storage layer uses `INSERT OR IGNORE`, forged blocks written at a given index can never be overwritten by a subsequent correct sync. A malicious ICRC-1 ledger canister operator can exploit this ordering to permanently inject a hash-chain-disconnected batch of fabricated blocks into Rosetta's local database, causing Rosetta to report fabricated account balances to downstream consumers.

## Finding Description

**Root cause — store before validate:**

`sync_blocks_interval` iterates from the certified tip downward. On each iteration:

1. `fetch_blocks_interval` retrieves a batch from the (attacker-controlled) canister. [1](#0-0) 
2. `blocks_verifier::is_valid_blockchain` checks internal chain consistency and that the highest block's hash equals `leading_block_hash` (the certified tip hash). This passes because the attacker crafted the forged chain to end at their certified hash. [2](#0-1) 
3. `leading_block_hash` is updated to the parent hash of the lowest fetched block. [3](#0-2) 
4. **`store_blocks` commits the batch to SQLite.** [4](#0-3) 
5. Only on the final iteration (when `next_index_interval.start() == sync_range.index_range.start()`), the trailing parent-hash check fires. If the forged chain's lowest block does not connect to the real stored chain, `bail!` is returned — but the forged blocks are **already committed**. [5](#0-4) 

**Why `INSERT OR IGNORE` makes the injection permanent:**

The storage layer uses `INSERT OR IGNORE` keyed on `idx`. Once forged blocks occupy indices N..N+M-1, every subsequent correct sync attempt silently drops the real blocks at those indices. [6](#0-5) 

**Why the certified-tip verification does not prevent this:**

`get_certified_chain_tip()` verifies the IC BLS certificate over the canister's `certified_data`. However, a canister sets its own `certified_data` via `ic0.certified_data_set()`. A malicious canister can certify an arbitrary `tip_hash`, and the subnet will faithfully sign a valid certificate over it. The BLS verification therefore provides no protection against a malicious canister operator. [7](#0-6) 

**Why `is_valid_blockchain` does not prevent this:**

`is_valid_blockchain` only checks (a) internal hash-chain consistency within the fetched batch and (b) that the last block's hash equals the certified tip hash. It does not check that the first block's parent hash connects to the already-stored chain. That connection check is the trailing-hash check, which is performed after storage. [8](#0-7) 

## Impact Explanation

After injection, `update_account_balances` processes the forged blocks and writes fabricated balances to the `account_balances` table. Any exchange or custodian querying Rosetta for balances receives phantom token amounts. This constitutes a **significant Rosetta security impact with concrete financial harm** to downstream consumers, matching the High ($2,000–$10,000) bounty tier: "Significant Chain Fusion, ck-token, ledger, Rosetta, boundary/API, XRC, Internet Identity, NNS, SNS, or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation

The attacker must control the ICRC-1 ledger canister that the target Rosetta instance is configured to sync from. Deploying a canister on the IC is permissionless. The realistic threat model is a malicious token issuer whose token an exchange has listed and whose Rosetta instance is pointed at the issuer's canister — a concrete, non-hypothetical deployment pattern. No special privileges beyond canister controllership are required, and the attack is repeatable across sync cycles.

## Recommendation

Move the trailing parent-hash check **before** the `store_blocks` call. Specifically, after computing `leading_block_hash = fetched_blocks[0].get_parent_hash()` (line 440), and when `*next_index_interval.start() == *sync_range.index_range.start()`, verify `leading_block_hash == sync_range.trailing_parent_hash` before calling `storage_client.store_blocks(...)`. Only store the batch if the check passes.

Alternatively, wrap the entire `sync_blocks_interval` loop body in a database transaction that is rolled back on any validation failure, including the trailing-hash check, eliminating the window in which forged blocks can be permanently committed.

## Proof of Concept

```
1. Sync N real blocks into Rosetta (indices 0..N-1).
2. Replace the Icrc1Agent with a mock that:
   a. Returns a certified tip at index N+M-1 with hash H_fake
      (canister sets certified_data = hash_tree{tip_hash: H_fake}).
   b. Returns M forged blocks [N..N+M-1] forming a valid internal chain
      ending at H_fake, but with forged_block[N].parent_hash ≠ hash(real block N-1).
3. Call sync_from_the_tip (or sync_blocks_interval directly).
4. is_valid_blockchain passes (internal consistency + leading hash == H_fake).
5. store_blocks commits forged blocks N..N+M-1 to SQLite via INSERT OR IGNORE.
6. Trailing-hash check fires bail! — but DB already contains forged blocks.
7. Call update_account_balances.
8. Assert fabricated balances appear in account_balances table.
9. Re-run sync with the real ledger agent; assert real blocks at N..N+M-1
   are silently dropped by INSERT OR IGNORE and fabricated balances persist.
```

### Citations

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

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L403-408)
```rust
        let fetched_blocks = fetch_blocks_interval(
            agent.clone(),
            next_index_interval.clone(),
            archive_canister_ids.clone(),
        )
        .await;
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L419-429)
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
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L440-440)
```rust
        leading_block_hash.clone_from(&fetched_blocks[0].get_parent_hash());
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L444-448)
```rust
        let result = storage_client.store_blocks(fetched_blocks.clone()).await;
        if let Err(e) = result {
            error!("Error while calling storage_client.store_blocks: {}", e);
            return Err(e);
        }
```

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L461-471)
```rust
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

**File:** rs/rosetta-api/icrc1/src/ledger_blocks_synchronization/blocks_synchronizer.rs (L692-724)
```rust
    pub fn is_valid_blockchain(
        blockchain: &[RosettaBlock],
        leading_block_hash: &ByteBuf,
    ) -> Result<(), String> {
        if blockchain.is_empty() {
            return Ok(());
        }

        // Check that the leading block has the block hash that is provided.
        // Safe to call unwrap as the blockchain is guaranteed to have at least one element.
        if blockchain.last().unwrap().clone().get_block_hash().clone() != leading_block_hash {
            return Err(format!(
                "Invalid block at index {}",
                blockchain.last().unwrap().clone().index
            ));
        }

        let mut parent_hash = Some(blockchain[0].clone().get_block_hash().clone());
        // The blockchain has more than one element so it is safe to skip the first one.
        // The first element cannot be verified so we start at element 2.
        for block in blockchain.iter().skip(1) {
            if block.get_parent_hash() != parent_hash {
                if block.index == 0 {
                    return Err("Block with index 0 found at different location".to_string());
                } else {
                    return Err(format!("Invalid block at index {}", block.index - 1));
                }
            }
            parent_hash = Some(block.clone().get_block_hash());
        }

        // No invalid blocks were found return true.
        Ok(())
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L686-688)
```rust
        insert_tx.prepare_cached(
        "INSERT OR IGNORE INTO blocks (idx, hash, serialized_block, parent_hash, timestamp,tx_hash,operation_type,from_principal,from_subaccount,to_principal,to_subaccount,spender_principal,spender_subaccount,memo,amount,expected_allowance,fee,transaction_created_at_time,approval_expires_at) VALUES (:idx, :hash, :serialized_block, :parent_hash, :timestamp,:tx_hash,:operation_type,:from_principal,:from_subaccount,:to_principal,:to_subaccount,:spender_principal,:spender_subaccount,:memo,:amount,:expected_allowance,:fee,:transaction_created_at_time,:approval_expires_at)")?
                    .execute(named_params! {
```
