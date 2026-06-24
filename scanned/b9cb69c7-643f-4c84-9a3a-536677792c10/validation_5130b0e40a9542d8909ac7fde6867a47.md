The code is fully readable. Let me trace the exact logic flow for the claimed scenario.

### Title
Logo-Omission Guard Bypassed in `convert_proposal` When `return_self_describing_action=true` — (`rs/nns/governance/src/pb/proposal_conversions.rs`)

### Summary

A logic defect in `convert_proposal` causes the `omit_large_fields` guard for `CreateServiceNervousSystem` logos to be permanently disabled whenever `return_self_describing_action=true`. Any unprivileged caller can trigger this via `list_proposals`, receiving full logo bytes in `self_describing_action` even when `omit_large_fields=Some(true)` is explicitly requested.

---

### Finding Description

`ProposalDisplayOptions::for_list_proposals` sets `show_action = !return_self_describing_action`: [1](#0-0) 

So when `return_self_describing_action=true`, `show_action=false`.

In `convert_proposal`, the `action` field is set to `None` when `show_action()` is false: [2](#0-1) 

Then `is_create_service_nervous_system_proposal` is derived from that already-`None` converted action: [3](#0-2) 

Because `action` is always `None` in this branch, `is_create_service_nervous_system_proposal` is always `false`. The logo-omission guard passed to `convert_self_describing_action` therefore evaluates to `false && true = false`: [4](#0-3) 

Inside `convert_self_describing_action`, when `omit_create_service_nervous_system_logos=false`, `paths_to_omit` is an empty `vec![]`, so the logo and token_logo fields pass through unfiltered: [5](#0-4) 

The `omit_create_service_nervous_system_large_fields()` method itself correctly returns `true` (since `omit_large_fields_requested=true` and `multi_query=true`): [6](#0-5) 

The bug is that the guard is short-circuited by the `is_create_service_nervous_system_proposal` flag, which is derived from the wrong source (the already-suppressed converted action, not the original `pb::Proposal` action).

---

### Impact Explanation

- `omit_large_fields=true` is intended to suppress large image blobs (logo, token_logo) in `CreateServiceNervousSystem` proposals to keep `list_proposals` responses within the IC's 2 MB message size limit.
- With this bug, any caller using `return_self_describing_action=true` (the newer API path) receives full logo bytes regardless of `omit_large_fields`.
- A `CreateServiceNervousSystem` proposal logo can be up to ~256 KiB (base64). With multiple such proposals in a single `list_proposals` response, the combined payload can exceed the IC's 2 MB query response limit, causing response serialization to fail and the query to trap.

---

### Likelihood Explanation

- `list_proposals` is a public, unauthenticated query call — any caller can set both flags.
- `CreateServiceNervousSystem` proposals are infrequent but persistent in governance state; a handful with large logos is sufficient to trigger oversized responses.
- The NNS mainnet has had multiple real `CreateServiceNervousSystem` proposals, each potentially carrying logo blobs.

---

### Recommendation

Derive `is_create_service_nervous_system_proposal` from the original `pb::Proposal` action field, not from the already-conditionally-suppressed converted `action`. For example, in `convert_proposal`:

```rust
let is_create_service_nervous_system_proposal = item.action.as_ref().is_some_and(|action| {
    matches!(action, pb::proposal::Action::CreateServiceNervousSystem(_))
});
```

This decouples the type-detection logic from the display-suppression logic.

---

### Proof of Concept

1. Submit a `CreateServiceNervousSystem` proposal with a 256 KiB base64-encoded logo.
2. Call `list_proposals` with `omit_large_fields=Some(true)` and `return_self_describing_action=Some(true)`.
3. Observe that the returned `self_describing_action.value` map contains the full logo bytes under the `"logo"` key (not `Null`).
4. With enough such proposals in the response window, the total response size exceeds 2 MB and the query traps.

The existing unit test `test_self_describing_value_omit_logos` only tests `convert_self_describing_action` in isolation with a hardcoded boolean — it does not exercise the `convert_proposal` integration path where `show_action=false` causes `is_create_service_nervous_system_proposal` to be `false`. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L17-27)
```rust
    pub fn for_list_proposals(
        omit_large_fields_requested: bool,
        return_self_describing_action: bool,
    ) -> Self {
        Self {
            omit_large_fields_requested,
            show_self_describing_action: return_self_describing_action,
            show_action: !return_self_describing_action,
            multi_query: true,
        }
    }
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L59-61)
```rust
    pub fn omit_create_service_nervous_system_large_fields(&self) -> bool {
        self.omit_large_fields_requested && self.multi_query
    }
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L317-328)
```rust
    let paths_to_omit = if omit_create_service_nervous_system_logos {
        vec![
            RecordPath {
                fields_names: vec!["logo"],
            },
            RecordPath {
                fields_names: vec!["ledger_parameters", "token_logo"],
            },
        ]
    } else {
        vec![]
    };
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L495-499)
```rust
    let action = if display_options.show_action() {
        action.as_ref().map(|x| convert_action(x, display_options))
    } else {
        None
    };
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L500-502)
```rust
    let is_create_service_nervous_system_proposal = action.as_ref().is_some_and(|action| {
        matches!(action, api::proposal::Action::CreateServiceNervousSystem(_))
    });
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L503-515)
```rust
    let self_describing_action = if display_options.show_self_describing_action() {
        self_describing_action
            .as_ref()
            .map(|self_describing_action| {
                convert_self_describing_action(
                    self_describing_action,
                    is_create_service_nervous_system_proposal
                        && display_options.omit_create_service_nervous_system_large_fields(),
                )
            })
    } else {
        None
    };
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L745-829)
```rust
    #[test]
    fn test_self_describing_value_omit_logos() {
        let create_service_nervous_system = CreateServiceNervousSystem {
            name: Some("some name".to_string()),
            logo: Some(Image {
                base64_encoding: Some("base64 encoding of a logo".to_string()),
            }),
            ledger_parameters: Some(LedgerParameters {
                token_name: Some("some token name".to_string()),
                token_logo: Some(Image {
                    base64_encoding: Some("base64 encoding of a token logo".to_string()),
                }),
                ..Default::default()
            }),
            ..Default::default()
        };
        let self_describing_action = SelfDescribingProposalAction {
            type_name: "Create Service Nervous System (SNS)".to_string(),
            type_description: "Create a new Service Nervous System (SNS).".to_string(),
            value: Some(pb::SelfDescribingValue::from(create_service_nervous_system)),
        };

        // Sanity check that the self-describing value does have logos when we don't omit them.
        let self_describing_value_with_logos =
            convert_self_describing_action(&self_describing_action, false)
                .value
                .unwrap();
        let map = match self_describing_value_with_logos {
            api::SelfDescribingValue::Map(map) => map,
            _ => panic!("Expected a map"),
        };
        assert_eq!(
            map.get("name").unwrap(),
            &api::SelfDescribingValue::Text("some name".to_string())
        );
        assert_eq!(
            map.get("logo").unwrap(),
            &api::SelfDescribingValue::Map(hashmap! {
                "base64_encoding".to_string() => api::SelfDescribingValue::Text("base64 encoding of a logo".to_string()),
            })
        );
        let ledger_parameters = map.get("ledger_parameters").unwrap();
        let ledger_parameters_map = match ledger_parameters {
            api::SelfDescribingValue::Map(map) => map,
            _ => panic!("Expected a map"),
        };
        assert_eq!(
            ledger_parameters_map.get("token_name").unwrap(),
            &api::SelfDescribingValue::Text("some token name".to_string())
        );
        assert_eq!(
            ledger_parameters_map.get("token_logo").unwrap(),
            &api::SelfDescribingValue::Map(hashmap! {
                "base64_encoding".to_string() => api::SelfDescribingValue::Text("base64 encoding of a token logo".to_string()),
            })
        );

        // Now check that the self-describing value does not have logos when we omit them, while the other fields are still present.
        let self_describing_value_without_logos =
            convert_self_describing_action(&self_describing_action, true)
                .value
                .unwrap();
        let map = match self_describing_value_without_logos {
            api::SelfDescribingValue::Map(map) => map,
            _ => panic!("Expected a map"),
        };
        assert_eq!(
            map.get("name").unwrap(),
            &api::SelfDescribingValue::Text("some name".to_string())
        );
        assert_eq!(map.get("logo"), Some(&api::SelfDescribingValue::Null));
        let ledger_parameters = map.get("ledger_parameters").unwrap();
        let ledger_parameters_map = match ledger_parameters {
            api::SelfDescribingValue::Map(map) => map,
            _ => panic!("Expected a map"),
        };
        assert_eq!(
            ledger_parameters_map.get("token_name").unwrap(),
            &api::SelfDescribingValue::Text("some token name".to_string())
        );
        assert_eq!(
            ledger_parameters_map.get("token_logo"),
            Some(&api::SelfDescribingValue::Null)
        );
    }
```
