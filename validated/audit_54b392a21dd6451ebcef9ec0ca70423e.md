Audit Report

## Title
Attacker Can Permanently Lose Legitimate ckETH Deposits by Flooding a Block with Zero-Value Deposits — (`rs/ethereum/cketh/minter/src/deposit.rs`, `rs/ethereum/cketh/minter/EthDepositHelper.sol`)

## Summary
The `deposit()` function in `EthDepositHelper.sol` accepts zero-value calls with no minimum `msg.value` guard, allowing any Ethereum EOA to emit thousands of valid `ReceivedEth` events at minimal cost. When a single block's `eth_getLogs` response exceeds the IC's 2 MB HTTP outcall limit and the binary search narrows to a singleton range `[N, N]`, the minter in `deposit.rs` unconditionally emits `SkippedBlockForContract` and advances `last_scraped_block_number` past that block with no retry or recovery path, permanently losing all legitimate deposits in that block.

## Finding Description
**Root cause 1 — `EthDepositHelper.sol` L32–35:** The `deposit()` function is `public payable` with no `require(msg.value > 0)`. A call with `msg.value == 0` succeeds, transfers 0 ETH to the minter address, and emits a fully valid `ReceivedEth(msg.sender, 0, _principal)` event carrying the exact topic (`0x257e057bb61920d8d0ed2cb7b720ac7f9c513cd1110bc9fa543079154f45f435`) that the minter's `eth_getLogs` filter matches. [1](#0-0) 

**Root cause 2 — `deposit.rs` L272–283:** When `from_block == to_block` and `is_response_too_large` is true, the minter emits `SkippedBlockForContract` and calls `S::update_last_scraped_block_number(s, to_block)`. There is no retry, no governance hook, and no flag that would cause the block to be re-examined. The block is permanently skipped. [2](#0-1) 

**Exploit flow:**
1. Attacker deploys a batch contract that calls `CkEthDeposit.deposit{value: 0}(principal)` ~3,500 times in a single transaction (~11.55 M gas, well within Ethereum's ~30 M block gas limit).
2. Each call emits one `ReceivedEth` log (~600 bytes in JSON). The resulting `eth_getLogs` JSON response exceeds 2 MB.
3. The minter binary-searches: `[N, N+k]` → too large → halved → … → `[N, N]` → still too large → `SkippedBlockForContract(N)`.
4. Any legitimate ETH deposit in block N is permanently lost: the ETH was already forwarded to the minter's Ethereum address by the helper contract, but ckETH is never minted.

The `ReceivedEthLogScraping::next_scrape` confirms the filter is scoped only to the helper contract address and the `RECEIVED_ETH_EVENT_TOPIC`, with no value filter. [3](#0-2) 

`DepositHelperWithSubaccount.sol` `depositEth()` has the identical missing guard and is equally exploitable. [4](#0-3) 

## Impact Explanation
Any ETH deposited to the helper contract in a flooded block is locked forever: the minter never mints ckETH for it, and the ETH cannot be recovered from the helper contract (it was already forwarded to the minter's Ethereum address). The `SkippedBlockForContract` event is recorded in the audit log but there is no on-chain or canister-level mechanism to re-process the block without a governance upgrade that manually rewinds `last_scraped_block_number`. This constitutes **permanent loss of in-scope chain-key/ledger assets (ckETH)**, matching the High impact class: "Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm." If targeted at a block containing a large deposit (>$1M), it escalates to Critical.

## Likelihood Explanation
- **Attacker capability:** Any Ethereum EOA. No privileged access required.
- **Cost:** ~$350 in gas per targeted block (11.55 M gas × 10 gwei × $3,000/ETH). A single batch transaction suffices.
- **Timing:** The attacker monitors the public Ethereum mempool for large deposits and front-runs or same-block-floods the target block.
- **No detection before damage:** The minter has no pre-skip validation distinguishing zero-value spam from legitimate deposits.
- **Repeatability:** The attack can be repeated for every block, or selectively targeted at high-value deposits.

## Recommendation
1. **Helper contracts:** Add `require(msg.value > 0, "zero deposit")` to `deposit()` in `EthDepositHelper.sol` and `depositEth()` in `DepositHelperWithSubaccount.sol`. Since these contracts are immutable on-chain, a new version must be deployed and the minter upgraded to point to the new address.
2. **Minter — do not permanently skip blocks without governance confirmation:** Replace the unconditional `SkippedBlockForContract` + `update_last_scraped_block_number` path with a quarantine that requires an explicit governance proposal to advance past the block, giving operators a chance to investigate and recover affected deposits.
3. **Minter — filter zero-value events at the RPC layer:** Consider adding a `value > 0` filter in the `eth_getLogs` request or post-parse filtering, though this alone does not fix the root cause since the response size check occurs at the HTTP layer before parsing.

## Proof of Concept
The existing test `should_skip_single_block_containing_too_many_events` in `rs/ethereum/cketh/minter/tests/cketh.rs` already validates the skip path end-to-end, confirming that 3,500 logs (~600 bytes each) exceed the 2 MB limit and result in `SkippedBlock` being emitted. [5](#0-4) 

Extending this test to include one legitimate deposit log in the same block and asserting that no `MintedCkEth` event is emitted constitutes a complete proof. The Solidity PoC batch contract requires no ETH balance and fits within a single Ethereum block's gas limit.

### Citations

**File:** rs/ethereum/cketh/minter/EthDepositHelper.sol (L32-35)
```text
    function deposit(bytes32 _principal) public payable {
        emit ReceivedEth(msg.sender, msg.value, _principal);
        cketh_minter_main_address.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L272-284)
```rust
                if e.has_http_outcall_error_matching(is_response_too_large) {
                    if from_block == to_block {
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::SkippedBlockForContract {
                                    contract_address,
                                    block_number: to_block,
                                },
                            );
                        });
                        mutate_state(|s| S::update_last_scraped_block_number(s, to_block));
                    } else {
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/scraping.rs (L52-61)
```rust
    fn next_scrape(state: &State) -> Option<Scrape> {
        let contract_address = *Self::contract_address(state)?;
        let last_scraped_block_number = Self::last_scraped_block_number(state);
        let topics = vec![Topic::Single(Hex32::from(RECEIVED_ETH_EVENT_TOPIC))];
        Some(Scrape {
            contract_address,
            last_scraped_block_number,
            topics,
        })
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L503-506)
```text
    function depositEth(bytes32 principal, bytes32 subaccount) public payable {
        emit ReceivedEthOrErc20(ZERO_ADDRESS, msg.sender, msg.value, principal, subaccount);
        minterAddress.transfer(msg.value);
    }
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L1086-1095)
```rust
#[test]
fn should_skip_single_block_containing_too_many_events() {
    let cketh = CkEthSetup::default();
    let deposit = DepositParams::default().to_log_entry();
    // around 600 bytes per log
    // we need at least 3334 logs to reach the 2MB limit
    let large_amount_of_logs = multi_logs_for_single_transaction(deposit.clone(), 3_500);
    assert!(serde_json::to_vec(&large_amount_of_logs).unwrap().len() > 2_000_000);

    cketh.env.advance_time(SCRAPING_ETH_LOGS_INTERVAL);
```
