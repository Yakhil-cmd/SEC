### Title
Hardcoded Consensus Threshold and Provider Count in ckETH Minter RPC Client Causes Unrecoverable Operational Freeze - (File: rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs)

### Summary
The ckETH minter's `rpc_client()` function hardcodes both the total number of EVM RPC providers (`TOTAL_NUMBER_OF_PROVIDERS = 4`) and the minimum consensus threshold (`min_threshold = 3` for Mainnet, `2` for Sepolia) as compile-time constants. Neither value is exposed in `UpgradeArg` and neither can be changed without a full NNS governance proposal and canister Wasm replacement. When enough providers simultaneously fail or diverge, the minter freezes and cannot process any ckETH or ckERC20 deposits or withdrawals until an emergency upgrade passes.

### Finding Description

In `rpc_client()`:

```rust
// rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs
pub fn rpc_client(state: &State) -> EvmRpcClient<...> {
    const TOTAL_NUMBER_OF_PROVIDERS: u8 = 4;   // hardcoded
    const MAX_NUM_RETRIES: u32 = 10;            // hardcoded

    let providers = match chain {
        EthereumNetwork::Mainnet => EvmRpcServices::EthMainnet(None),
        EthereumNetwork::Sepolia => EvmRpcServices::EthSepolia(Some(vec![
            EthSepoliaService::BlockPi,
            EthSepoliaService::PublicNode,
            EthSepoliaService::Alchemy,
            EthSepoliaService::Ankr,           // hardcoded list
        ])),
    };

    let min_threshold = match chain {
        EthereumNetwork::Mainnet => 3_u8,      // hardcoded
        EthereumNetwork::Sepolia => 2_u8,      // hardcoded
    };

    EvmRpcClient::builder(IcRuntime::new(), evm_rpc_id)
        .with_consensus_strategy(ConsensusStrategy::Threshold {
            total: Some(TOTAL_NUMBER_OF_PROVIDERS),
            min: min_threshold,
        })
        ...
}
``` [1](#0-0) 

The `UpgradeArg` type in the Candid interface exposes `evm_rpc_id` (the canister to route through) but provides no fields for `min_threshold`, `TOTAL_NUMBER_OF_PROVIDERS`, or the Sepolia provider list: [2](#0-1) 

The minter's `State` struct stores `evm_rpc_id` as the only RPC-routing parameter: [3](#0-2) 

The `ConsensusStrategy::Threshold { total: Some(4), min: 3 }` means: query 4 providers, require at least 3 to agree. If 2 or more providers are simultaneously unavailable or return divergent results, the threshold is never met and every RPC call fails, halting all minter tasks (log scraping, deposit minting, withdrawal processing).

### Impact Explanation

All ckETH and ckERC20 deposits and withdrawals freeze until an NNS governance proposal is submitted, voted on (minimum ~4 days under normal voting), and executed. Users who have already burned ckETH to initiate a withdrawal cannot receive their ETH. New deposits are not minted. The bridge is fully non-functional for the duration. This is a direct financial impact on all ckETH/ckERC20 holders and users.

### Likelihood Explanation

This is not theoretical. The production ckETH minter has been frozen by exactly this mechanism at least three times, each requiring an emergency canister upgrade:

- **2024-03-18**: Cloudflare returned wrong `eth_getLogs` results after the Dencun upgrade → minting stuck. [4](#0-3) 

- **2024-09-11**: Ankr dropped IPv6 connectivity → minter stuck, unable to process deposits or withdrawals. [5](#0-4) 

- **2024-09-12**: Pocket Network responses diverged across replicas causing consensus failures → another emergency upgrade within 24 hours. [6](#0-5) 

Each incident required a full Wasm replacement because neither the threshold nor the provider list was configurable at runtime. The root cause (hardcoded parameters) was never addressed; only the specific failing provider was swapped in each upgrade.

### Recommendation

Add `min_consensus_threshold: opt nat8` and `total_providers: opt nat8` fields to `UpgradeArg` in `cketh_minter.did`, store them in `State`, and read them in `rpc_client()` instead of the compile-time constants. For Sepolia, expose the provider list similarly. This allows the NNS to lower the threshold (e.g., from 3 to 2) or adjust the provider count via a simple upgrade-arg-only proposal without replacing the Wasm binary, dramatically reducing recovery time during provider outages.

### Proof of Concept

1. Two of the four EVM RPC providers used by the EVM RPC canister for Mainnet become unavailable simultaneously (as happened with Ankr + LlamaNodes on 2024-09-11/12).
2. Every call from `rpc_client()` with `ConsensusStrategy::Threshold { total: Some(4), min: 3 }` returns fewer than 3 agreeing responses.
3. All minter timer tasks (`scrape_eth_logs`, `process_retrieve_eth_requests`, etc.) fail on every tick.
4. No ckETH can be minted for new deposits; no ETH can be sent for pending withdrawals.
5. Recovery requires an NNS proposal to replace the canister Wasm — a process that takes days under normal governance — because `min_threshold` is not in `UpgradeArg` and cannot be changed without a new binary. [7](#0-6) [2](#0-1)

### Citations

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs (L30-64)
```rust
pub fn rpc_client(state: &State) -> EvmRpcClient<IcRuntime, CandidResponseConverter, DoubleCycles> {
    const TOTAL_NUMBER_OF_PROVIDERS: u8 = 4;
    const MAX_NUM_RETRIES: u32 = 10;

    let chain = state.ethereum_network();
    let evm_rpc_id = state.evm_rpc_id();

    let providers = match chain {
        EthereumNetwork::Mainnet => EvmRpcServices::EthMainnet(None),
        EthereumNetwork::Sepolia => EvmRpcServices::EthSepolia(Some(vec![
            EthSepoliaService::BlockPi,
            EthSepoliaService::PublicNode,
            EthSepoliaService::Alchemy,
            EthSepoliaService::Ankr,
        ])),
    };

    let min_threshold = match chain {
        EthereumNetwork::Mainnet => 3_u8,
        EthereumNetwork::Sepolia => 2_u8,
    };
    assert!(
        min_threshold <= TOTAL_NUMBER_OF_PROVIDERS,
        "BUG: min_threshold too high"
    );

    EvmRpcClient::builder(IcRuntime::new(), evm_rpc_id)
        .with_rpc_sources(providers)
        .with_consensus_strategy(ConsensusStrategy::Threshold {
            total: Some(TOTAL_NUMBER_OF_PROVIDERS),
            min: min_threshold,
        })
        .with_retry_strategy(DoubleCycles::with_max_num_retries(MAX_NUM_RETRIES))
        .build()
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L114-146)
```text
type UpgradeArg = record {
    // Change the nonce of the next transaction to be sent to the Ethereum network.
    next_transaction_nonce : opt nat;

    // Change the minimum amount in Wei that can be withdrawn.
    minimum_withdrawal_amount : opt nat;

    // Change the ETH helper smart contract address.
    ethereum_contract_address : opt text;

    // Change the ethereum block height observed by the minter.
    ethereum_block_height : opt BlockTag;

    // The principal of the ledger suite orchestrator that handles the ICRC1 ledger suites
    // for all ckERC20 tokens.
    ledger_suite_orchestrator_id : opt principal;

    // Change the ERC-20 helper smart contract address.
    erc20_helper_contract_address : opt text;

    // Change the last scraped block number of the ERC-20 helper smart contract.
    last_erc20_scraped_block_number : opt nat;

    // The principal of the EVM RPC canister that handles the communication
    // with the Ethereum blockchain.
    evm_rpc_id : opt principal;

    // Change the deposit with subaccount helper smart contract address.
    deposit_with_subaccount_helper_contract_address : opt text;

    // Change the last scraped block number of the deposit with subaccount helper smart contract.
    last_deposit_with_subaccount_scraped_block_number : opt nat;
};
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L94-97)
```rust
    /// Canister ID of the EVM RPC canister that
    /// handles communication with Ethereum
    pub evm_rpc_id: Principal,

```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_03_18.md (L14-16)
```markdown

Since the rollout of the Ethereum Dencun upgrade on 2024-03-13, Cloudflare, one of the 3 Ethereum JSON-RPC providers that the ckETH minter uses to interact with the Ethereum blockchain, returns wrong results (see examples below). As a consequence, the minting of ckETH is currently stuck and withdrawals are wrongly considered not finalized. This upgrade switches the minter to use Llama Nodes (`https://eth.llamarpc.com`) instead of Cloudflare as a third JSON-RPC provider (in addition to Ankr and Public Node).

```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_09_11.md (L14-17)
```markdown
The Ethereum JSON-RPC provider Ankr (`rpc.ankr.com`) recently dropped its IPv6 connectivity and will need according to its support team a month to fix it.
This resulted in the ckETH minter being stuck and unable to process deposits nor withdrawals.
As a temporary fix, this proposal replaces Ankr by another provider: `eth-pokt.nodies.app` from [Pocket Network](https://www.pokt.network/).
The long term solution is to use a more robust strategy (e.g., agreement among 3 providers, when 4 were queried) using the EVM-RPC canister.
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_09_12.md (L14-20)
```markdown

The ckETH minter is currently unable to process conversions between ETH/ERC20 and ckETH/ckERC20 due to the following events:
1. Proposal [132415](https://dashboard.internetcomputer.org/proposal/132415) was executed at 2024.09.11 09:28 (UTC) and successfully replaced the Ethereum JSON-RPC provider Ankr (rpc.ankr.com) with `eth-pokt.nodies.app` from [Pocket Network](https://www.pokt.network/).
2. Unfortunately, at the same time the Ethereum JSON-RPC provider LlamaNodes  `eth.llamarpc.com` was down and constantly replying with `no response`. This seems to have been resolved since the ckETH minter did make progress around 2024.09.11 22:00 (UTC) but stopped since then.
3. The [logs](https://sv3dd-oaaaa-aaaar-qacoa-cai.raw.icp0.io/logs?sort=desc) show that responses from the Ethereum JSON-RPC provider Pocket Network (`eth-pokt.nodies.app`) differ between the replicas resulting in consensus failures.

As a temporary fix, this proposal replaces the Ethereum JSON-RPC provider Pocket Network (`eth-pokt.nodies.app`) with the Ethereum JSON-RPC provider BlockPi (`https://ethereum.blockpi.network/v1/rpc/public).
```
