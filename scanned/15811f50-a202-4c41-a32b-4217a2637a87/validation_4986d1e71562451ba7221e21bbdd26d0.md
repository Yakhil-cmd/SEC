### Title
Secp256k1 Identity Private Key Exposed as Command-Line Argument - (File: `rs/boundary_node/rate_limits/canister_client/src/main.rs`)

---

### Summary

The `rate-limiting-canister-client` binary accepts a raw secp256k1 private key (SEC1 PEM-encoded) directly as the `--identity-key` command-line argument. On Linux, any local user can read another process's full argument list from `/proc/<pid>/cmdline`, exposing the private key to any unprivileged user on the same host.

---

### Finding Description

The `Cli` struct in `rs/boundary_node/rate_limits/canister_client/src/main.rs` defines `identity_key` as a plain `Option<String>` CLI argument: [1](#0-0) 

The value is consumed directly as raw key material: [2](#0-1) 

The full SEC1 PEM private key string (e.g. `-----BEGIN EC PRIVATE KEY-----\nMHQCAQEE...`) is therefore present verbatim in the process argument vector for the entire lifetime of the process. On Linux, `/proc/<pid>/cmdline` is world-readable by default (unless `hidepid=2` is set on the `proc` filesystem mount, which is not the default on most distributions).

A parallel pattern exists in `rs/sys/src/utility_command.rs`, where the HSM PIN is passed as a `--pin` argument to a spawned `pkcs11-tool` subprocess: [3](#0-2) 

This subprocess's arguments are also visible in `/proc`. The same PIN is accepted as a CLI argument by `ic-admin` (`rs/registry/admin/bin/main.rs`): [4](#0-3) 

The primary and most severe instance is the `rate-limiting-canister-client` because it exposes the actual signing private key, not just a PIN to a hardware device.

---

### Impact Explanation

The `identity_key` is a secp256k1 private key used to authenticate calls to the boundary node rate-limiting canister via `Secp256k1Identity`. An attacker who obtains this key can:

1. Submit arbitrary rate-limiting rule configurations to the rate-limiting canister, potentially blocking legitimate IC users or whitelisting malicious traffic at the boundary layer.
2. Impersonate the authorized operator identity for any future canister calls that accept this principal.

The rate-limiting canister governs which API requests are throttled at IC boundary nodes, making unauthorized write access a service-availability and security-policy bypass risk.

---

### Likelihood Explanation

Any unprivileged local user account on the host running `rate-limiting-canister-client` can poll `/proc` (e.g. with `pspy` or a simple `cat /proc/<pid>/cmdline` loop) and capture the key the moment the process starts. No elevated privileges are required. Automation scripts that invoke the binary with `--identity-key` inline (a common pattern in CI/CD pipelines) make the window of exposure repeatable and predictable.

---

### Recommendation

- Remove `--identity-key` as a CLI string argument. Instead, accept a **path to a PEM file** (analogous to how `ic-admin` uses `--secret-key-pem` with a `PathBuf`): [5](#0-4) 
- For the HSM PIN in `ic-admin` and `UtilityCommand::sign_message`, read the PIN from an environment variable or an interactive prompt (as already done in `rs/nervous_system/tools/submit-motion-proposal/src/main.rs` via `std::env::var("DFX_HSM_PIN")`), rather than passing it as a subprocess argument. [6](#0-5) 

---

### Proof of Concept

```bash
# On a multi-user Linux host, as an unprivileged attacker:
# 1. Start monitoring processes
pspy64 &

# 2. Victim operator runs the rate-limiting client:
rate-limiting-canister-client \
  --canister-id <CANISTER_ID> \
  --config-file rules.json \
  --identity-key "-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEI..."

# 3. Attacker reads the key directly from /proc:
cat /proc/$(pgrep rate-limiting)/cmdline | tr '\0' '\n' | grep -A1 identity-key
# Output: -----BEGIN EC PRIVATE KEY-----\nMHQCAQEEI...

# 4. Attacker uses the stolen key to submit malicious rate-limiting rules:
rate-limiting-canister-client \
  --canister-id <CANISTER_ID> \
  --config-file malicious_rules.json \
  --identity-key "<STOLEN_KEY>"
```

The stolen key grants full write access to the rate-limiting canister under the victim's authorized principal identity.

### Citations

**File:** rs/boundary_node/rate_limits/canister_client/src/main.rs (L24-27)
```rust
    /// Identity key
    #[arg(long)]
    identity_key: Option<String>,

```

**File:** rs/boundary_node/rate_limits/canister_client/src/main.rs (L61-67)
```rust
        let identity_key = cli.identity_key.unwrap();
        let canister_id = cli.canister_id.unwrap();

        // create the agent
        let identity = Secp256k1Identity::from_private_key(
            SecretKey::from_sec1_pem(&identity_key).context("failed to parse the identity key")?,
        );
```

**File:** rs/sys/src/utility_command.rs (L115-138)
```rust
    pub fn sign_message(
        msg: Vec<u8>,
        hsm_slot: Option<&str>,
        pin: Option<&str>,
        key_id: Option<&str>,
    ) -> Self {
        Self::new(
            "pkcs11-tool".to_string(),
            vec![
                "--slot",                // choose HSM slot
                hsm_slot.unwrap_or("0"), // default: 0
                "--pin",                 //
                pin.unwrap_or("358138"), // default:
                "--sign",                // operation
                "--id",                  //
                key_id.unwrap_or("01"),  // default: 01
                "--mechanism",
                "ECDSA",
            ]
            .into_iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>(),
        )
        .with_input(ic_crypto_sha2::Sha256::hash(msg.as_slice()).to_vec())
```

**File:** rs/registry/admin/bin/main.rs (L207-210)
```rust
    #[clap(short = 's', long, global = true)]
    /// The pem file containing a secret key to use while authenticating with
    /// the NNS.
    secret_key_pem: Option<PathBuf>,
```

**File:** rs/registry/admin/bin/main.rs (L238-246)
```rust
    /// The PIN used to unlock the HSM.
    #[clap(
        long = "pin",
        help = "Only required if use-hsm is set. Ignored otherwise.",
        global = true,
        requires = "use_hsm",
        visible_alias = "pin"
    )]
    hsm_pin: Option<String>,
```

**File:** rs/nervous_system/tools/submit-motion-proposal/src/main.rs (L232-240)
```rust
        || {
            // Get pin from environment variable.
            std::env::var("DFX_HSM_PIN").map_err(|err| {
                format!(
                    "DFX_HSM_PIN environment variable is not set (or just \
                     not exported such that it is visible to this process): {err}",
                )
            })
        },
```
