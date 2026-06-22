### Title
ckERC20 Minter Internal Balance Tracking Does Not Account for Rebasing ERC-20 Tokens, Causing Permanent Loss of Rebase Rewards - (File: rs/ethereum/cketh/minter/src/state.rs)

### Summary
The ckETH minter's `Erc20Balances` struct tracks ERC-20 token balances exclusively from audit events (deposits and withdrawals). If a rebasing ERC-20 token (e.g., stETH, aTokens) is added as a supported ckERC20 token via NNS governance, the minter's Ethereum address accumulates additional tokens through rebasing that are never reflected in the ckERC20 total supply. These tokens become permanently irrecoverable because the minter has no mechanism to detect, account for, or distribute rebase-accrued balances.

### Finding Description

The `Erc20Balances` struct in `rs/ethereum/cketh/minter/src/state.rs` is explicitly described as "Computed based on audit events": [1](#0-0) 

The balance is updated in exactly two places:

1. `update_balance_upon_deposit` — adds the exact deposited amount from the `ReceivedErc20Event`: [2](#0-1) 

2. `update_balance_upon_withdrawal` — subtracts the exact withdrawn amount on finalization: [3](#0-2) 

The minting step in `rs/ethereum/cketh/minter/src/deposit.rs` mints exactly `event.value()` — the amount recorded in the deposit log event — not the current on-chain balance: [4](#0-3) 

The `add_ckerc20_token` endpoint in `rs/ethereum/cketh/minter/src/main.rs` only checks that the caller is the ledger suite orchestrator; it performs no validation of the token's economic properties: [5](#0-4) 

`record_add_ckerc20_token` in `rs/ethereum/cketh/minter/src/state.rs` only validates network match, symbol uniqueness, and ledger ID/address uniqueness — no rebasing check: [6](#0-5) 

The `Erc20Balances` struct has no `sync` or `refresh` path that queries the actual on-chain balance: [7](#0-6) 

Adding a new ckERC20 token requires an NNS upgrade proposal targeting the ledger suite orchestrator (LSO), which then calls `add_ckerc20_token` on the minter. The README explicitly describes this flow and notes that the address "MUST be a valid Ethereum address corresponding to an ERC-20 smart contract as specified in EIP-20" — with no restriction on rebasing behavior: [8](#0-7) 

### Impact Explanation

If a rebasing ERC-20 token (e.g., stETH at `0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84`) is added as a supported ckERC20 token:

1. Users deposit `N` stETH via the helper contract. The minter observes the `ReceivedErc20` log event and mints exactly `N` ckstETH.
2. Over time, stETH rebases: the minter's Ethereum address now holds `N + R` stETH (where `R` is the accumulated rebase reward), but the ckstETH total supply remains `N`.
3. Users can only burn ckstETH to withdraw stETH. Since the ckstETH total supply is `N`, only `N` stETH can ever be withdrawn.
4. The `R` stETH rebase rewards are permanently locked at the minter's Ethereum address with no recovery path — no admin function, no governance proposal, no sweep mechanism exists.

The impact is a **ledger conservation bug**: the minter's Ethereum address holds more ERC-20 tokens than the corresponding ckERC20 total supply represents, and the difference grows monotonically over time. The `erc20_balances` metric and `MinterInfo.erc20_balances` field will also permanently diverge from the actual on-chain balance, corrupting accounting.

### Likelihood Explanation

Adding a rebasing token requires an NNS governance proposal — a legitimate, non-malicious governance action. Rebasing tokens (stETH, wstETH unwrapped, aTokens) are among the most widely held ERC-20 assets. There is no technical barrier in the minter or orchestrator that would prevent such a proposal from being submitted and passing. The minter provides no warning, no documentation, and no on-chain check to signal that rebasing tokens are unsupported. A well-intentioned NNS proposal to add ckstETH would silently introduce this permanent fund-loss condition.

### Recommendation

1. **Explicit prohibition**: In `add_ckerc20_token` / `record_add_ckerc20_token`, document and enforce that rebasing tokens are not supported. Since on-chain detection of rebasing is not straightforward, a governance-level allowlist or a human-reviewed checklist in the proposal template should be mandated.
2. **Use non-rebasing wrappers**: Require that only non-rebasing wrappers (e.g., wstETH instead of stETH) are added as supported ckERC20 tokens.
3. **Share-based accounting**: If rebasing tokens must be supported, track user shares relative to the total pool balance rather than absolute deposited amounts, so rebase rewards are proportionally distributed to all ckERC20 holders.

### Proof of Concept

1. Submit an NNS upgrade proposal for the ledger suite orchestrator with `AddErc20Arg` specifying stETH (`0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84`, chain_id=1).
2. The LSO calls `add_ckerc20_token` on the minter; `record_add_ckerc20_token` succeeds with no rebasing check.
3. User A deposits 100 stETH via the helper contract. The minter scrapes the `ReceivedErc20` log, calls `erc20_add(stETH_address, 100)`, and mints 100 ckstETH to User A.
4. After one year, stETH rebases ~4%. The minter's Ethereum address now holds ~104 stETH. `erc20_balances.balance_of(stETH_address)` still returns 100 (no rebase event was ever scraped).
5. User A burns 100 ckstETH and withdraws 100 stETH. The remaining ~4 stETH rebase reward is permanently locked at the minter's Ethereum address. No function in `rs/ethereum/cketh/minter/src/main.rs` or any governance path can recover it. [9](#0-8) [2](#0-1) [10](#0-9) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L70-76)
```rust
    /// Current balance of ETH held by the minter.
    /// Computed based on audit events.
    pub eth_balance: EthBalance,

    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-339)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L377-383)
```rust
        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L398-424)
```rust
    pub fn record_add_ckerc20_token(&mut self, ckerc20_token: CkErc20Token) {
        assert_eq!(
            self.ethereum_network, ckerc20_token.erc20_ethereum_network,
            "ERROR: Expected {}, but got {}",
            self.ethereum_network, ckerc20_token.erc20_ethereum_network
        );
        let ckerc20_with_same_symbol = self
            .supported_ck_erc20_tokens()
            .filter(|ckerc20| ckerc20.ckerc20_token_symbol == ckerc20_token.ckerc20_token_symbol)
            .collect::<Vec<_>>();
        assert_eq!(
            ckerc20_with_same_symbol,
            vec![],
            "ERROR: ckERC20 token symbol {} is already used by {:?}",
            ckerc20_token.ckerc20_token_symbol,
            ckerc20_with_same_symbol
        );
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L729-771)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct Erc20Balances {
    balance_by_erc20_contract: BTreeMap<Address, Erc20Value>,
}

impl Erc20Balances {
    pub fn balance_of(&self, erc20_contract: &Address) -> Erc20Value {
        *self
            .balance_by_erc20_contract
            .get(erc20_contract)
            .unwrap_or(&Erc20Value::ZERO)
    }

    pub fn erc20_add(&mut self, erc20_contract: Address, deposit: Erc20Value) {
        match self.balance_by_erc20_contract.get(&erc20_contract) {
            Some(previous_value) => {
                let new_value = previous_value.checked_add(deposit).unwrap_or_else(|| {
                    panic!("BUG: overflow when adding {deposit} to {previous_value}")
                });
                self.balance_by_erc20_contract
                    .insert(erc20_contract, new_value);
            }
            None => {
                self.balance_by_erc20_contract
                    .insert(erc20_contract, deposit);
            }
        }
    }

    pub fn erc20_sub(&mut self, erc20_contract: Address, withdrawal_amount: Erc20Value) {
        let previous_value = self
            .balance_by_erc20_contract
            .get(&erc20_contract)
            .expect("BUG: Cannot subtract from a missing ERC-20 balance");
        let new_value = previous_value
            .checked_sub(withdrawal_amount)
            .unwrap_or_else(|| {
                panic!("BUG: underflow when subtracting {withdrawal_amount} from {previous_value}")
            });
        self.balance_by_erc20_contract
            .insert(erc20_contract, new_value);
    }
}
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-82)
```rust
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-574)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/README.adoc (L99-102)
```text
. `contract`: Uniquely identifies the ERC-20 smart contract.
.. `chain_id = 1`: designates Ethereum mainnet. This value MUST be `1`.
.. `address = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"`: address of the ERC-20 smart contract on Ethereum mainnet. The address MUST be a valid Ethereum address corresponding to an ERC-20 smart contract as specified in https://eips.ethereum.org/EIPS/eip-20[EIP-20].
. `ledger_init_arg`: Initialization arguments for the ledger that will be spawned off by the orchestrator.
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L28-30)
```rust
        EventType::AcceptedErc20Deposit(erc20_event) => {
            state.record_event_to_mint(&erc20_event.clone().into());
        }
```
