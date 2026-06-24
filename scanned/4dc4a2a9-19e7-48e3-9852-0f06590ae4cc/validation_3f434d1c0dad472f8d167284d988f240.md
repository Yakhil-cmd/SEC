### Title
Missing Pause/Emergency-Stop Mechanism in the ckETH Minter - (`rs/ethereum/cketh/minter/src/main.rs`)

### Summary
The ckETH minter canister (`sv3dd-oaaaa-aaaar-qacoa-cai`) handles ETH/ERC-20 ↔ ckETH/ckERC-20 conversions and controls threshold-ECDSA-signed Ethereum transactions. Unlike the ckBTC and ckDOGE minters, the ckETH minter has no operational `Mode` field (no `ReadOnly`, `RestrictedTo`, or `DepositsRestrictedTo` variant) and no equivalent emergency-stop mechanism. Any non-anonymous principal can call `withdraw_eth` or `withdraw_erc20` at any time, and there is no governance-accessible path to halt these operations without a full canister upgrade proposal — which takes days to pass through NNS voting.

### Finding Description
The ckBTC minter defines a `Mode` enum with `ReadOnly`, `RestrictedTo(Vec<Principal>)`, `DepositsRestrictedTo(Vec<Principal>)`, and `GeneralAvailability` variants. Both `update_balance` (deposit) and `retrieve_btc` (withdrawal) check `state.mode.is_deposit_available_for(caller)` and `state.mode.is_withdrawal_available_for(caller)` before proceeding. The mode can be changed at any time via an `UpgradeArgs` field, allowing the NNS to pass a lightweight upgrade proposal that sets `mode = ReadOnly` and immediately halts all minting and burning.

The ckDOGE minter reuses the same `Mode` type from `ic_ckbtc_minter::state::Mode` and has the same protection.

The ckETH minter's `UpgradeArg` struct contains no `mode` field. Its `State` struct contains no `mode` field. The `withdraw_eth` handler checks only: (1) caller is not anonymous, (2) destination address is not blocklisted, (3) amount ≥ minimum, and (4) a per-principal concurrency guard. There is no check that allows the NNS or any governance actor to halt withdrawals or deposits without deploying a new Wasm binary through a full NNS proposal.

### Impact Explanation
If a critical vulnerability is discovered in the ckETH minter (e.g., a double-mint bug, an incorrect nonce handling issue, or a compromised EVM-RPC response being accepted), the only available response is to submit a full NNS canister upgrade proposal. NNS proposals require a voting period that typically spans multiple days before reaching a decision. During this window, an attacker can continue to drain ETH from the minter's tECDSA-controlled Ethereum address or mint unbacked ckETH tokens. The minter controls real ETH on Ethereum via threshold ECDSA; any exploit that causes it to issue unauthorized `eth_sendRawTransaction` calls results in irreversible loss of funds. The ckBTC minter's `ReadOnly` mode was specifically designed to address this scenario and is already proven to work in production.

### Likelihood Explanation
The ckETH minter is the most complex chain-fusion canister in the repository: it scrapes Ethereum logs via the EVM-RPC canister (an external dependency), processes EIP-1559 transactions, handles ckERC-20 multi-token withdrawals, and manages reimbursement flows. The attack surface is large. The absence of a pause mechanism is a design gap that is directly analogous to the Gravity.sol finding: the system can process withdrawals continuously with no way to interrupt them short of a multi-day governance vote.

### Recommendation
Add a `mode: Mode` field to the ckETH minter's `State` struct (mirroring the `Mode` enum already defined in `rs/bitcoin/ckbtc/minter/src/state.rs`) and a corresponding `mode: opt Mode` field to `UpgradeArg`. Add mode checks at the entry points of `withdraw_eth` and `withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs`, and optionally in the log-scraping timer (to halt minting). This allows the NNS to pass a lightweight upgrade proposal with `UpgradeArg { mode: opt ReadOnly }` that takes effect immediately upon canister upgrade, halting all fund movements within a single round.

### Proof of Concept

**ckBTC minter — has mode protection:**

`rs/bitcoin/ckbtc/minter/src/state.rs` defines the `Mode` enum: [1](#0-0) 

`Mode::is_withdrawal_available_for` enforces it: [2](#0-1) 

`UpgradeArgs` exposes `mode: opt Mode` so the NNS can flip it without a code change: [3](#0-2) 

**ckETH minter — no mode field in `UpgradeArg`:**

The full `UpgradeArg` for the ckETH minter contains no `mode` field: [4](#0-3) 

**ckETH minter — `withdraw_eth` performs no mode/pause check:**

The `withdraw_eth` handler checks only anonymity, blocklist, amount floor, and a concurrency guard — no operational mode check: [5](#0-4) 

**ckETH minter — `State` struct has no `mode` field:**

The `State` struct in `rs/ethereum/cketh/minter/src/state.rs` contains no `mode` field, confirming the absence of any pause mechanism at the state level: [6](#0-5) 

**ckDOGE minter — also has mode protection (for comparison):**

The ckDOGE minter reuses the same `Mode` type and exposes it in `UpgradeArgs`: [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L339-353)
```rust
/// Controls which operations the minter can perform.
#[derive(
    Default, Clone, Eq, PartialEq, Debug, Serialize, candid::CandidType, serde::Deserialize,
)]
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    /// Only the specified principals can modify the minter's state.
    RestrictedTo(Vec<Principal>),
    /// Only the specified principals can deposit BTC.
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    /// No restrictions on the minter interactions.
    GeneralAvailability,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L376-388)
```rust
    /// Returns Ok if the specified principal can convert ckBTC to BTC.
    pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability | Self::DepositsRestrictedTo(_) => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC withdrawals are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L264-266)
```text
    /// If set, overrides the current minter's operation mode.
    mode : opt Mode;

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-297)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

```

**File:** rs/ethereum/cketh/minter/src/state.rs (L54-103)
```rust
pub struct State {
    pub ethereum_network: EthereumNetwork,
    pub ecdsa_key_name: String,
    pub cketh_ledger_id: Principal,
    pub log_scrapings: LogScrapings,
    pub ecdsa_public_key: Option<EcdsaPublicKeyResult>,
    pub cketh_minimum_withdrawal_amount: Wei,
    pub ethereum_block_height: CandidBlockTag,
    pub first_scraped_block_number: BlockNumber,
    pub last_observed_block_number: Option<BlockNumber>,
    pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
    pub minted_events: BTreeMap<EventSource, MintedEvent>,
    pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
    pub eth_transactions: EthTransactions,
    pub skipped_blocks: BTreeMap<Address, BTreeSet<BlockNumber>>,

    /// Current balance of ETH held by the minter.
    /// Computed based on audit events.
    pub eth_balance: EthBalance,

    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,

    /// Per-principal lock for pending withdrawals
    pub pending_withdrawal_principals: BTreeSet<Principal>,

    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: HashSet<TaskType>,

    /// Number of HTTP outcalls since the last upgrade.
    /// Used to correlate request and response in logs.
    pub http_request_counter: u64,

    pub last_transaction_price_estimate: Option<(u64, GasFeeEstimate)>,

    /// Canister ID of the ledger suite orchestrator that
    /// can add new ERC-20 token to the minter
    pub ledger_suite_orchestrator_id: Option<Principal>,

    /// Canister ID of the EVM RPC canister that
    /// handles communication with Ethereum
    pub evm_rpc_id: Principal,

    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
    /// - value: ckERC20 token symbol
    pub ckerc20_tokens: DedupMultiKeyMap<Principal, Address, CkTokenSymbol>,
}
```

**File:** rs/dogecoin/ckdoge/minter/src/lifecycle/upgrade.rs (L26-28)
```rust
    /// The mode in which the minter is running.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<Mode>,
```
