// Purpose: Launch the packaged Tauri desktop shell and supervise the bundled Next.js standalone sidecar.
// Scope: Sidecar process startup, readiness polling, window handoff, and orderly shutdown.
// Dependencies: Tauri runtime, the bundled Node executable and standalone UI resources, and reqwest for loopback readiness checks.

use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use reqwest::blocking::Client;
use tauri::{App, AppHandle, Manager, RunEvent, Url};

const SIDECAR_HOST: &str = "127.0.0.1";
const SIDECAR_PORT: u16 = 3210;
const SIDECAR_STARTUP_TIMEOUT: Duration = Duration::from_secs(45);
const SIDECAR_POLL_INTERVAL: Duration = Duration::from_millis(500);
const DEV_UI_BUILD_COMMAND: [&str; 3] = ["--filter", "@accounting-ai-agent/desktop-ui", "build"];
const DESKTOP_SHELL_ENV_FILE_NAME: &str = "desktop-shell.env";
const REMOTE_FRONTEND_URL_ENV_KEY: &str = "ACCOUNTING_AGENT_DESKTOP_REMOTE_URL";
const DEFAULT_API_BASE_PATH: &str = "/api";
const DEFAULT_API_HOST: &str = "127.0.0.1";
const DEFAULT_API_PORT: &str = "8000";
const DEFAULT_DATABASE_HOST: &str = "127.0.0.1";
const DEFAULT_DATABASE_PORT: &str = "5432";
const DEFAULT_REDIS_BROKER_URL: &str = "redis://127.0.0.1:6379/0";
const DEFAULT_STORAGE_ENDPOINT: &str = "127.0.0.1:9000";
const DEFAULT_STORAGE_SECURE: &str = "false";
const DEFAULT_SESSION_COOKIE_NAME: &str = "accounting_agent_session";

#[derive(Default)]
struct UiSidecarState {
    child: Mutex<Option<Child>>,
}

fn main() {
    let app = tauri::Builder::default()
        .manage(UiSidecarState::default())
        .setup(|app| {
            if let Some(remote_frontend_url) = resolve_remote_frontend_url(app)? {
                let main_window = app
                    .get_webview_window("main")
                    .ok_or("The main desktop window is missing from the Tauri configuration.")?;
                let parsed_remote_url = Url::parse(&remote_frontend_url).map_err(|error| {
                    format!(
                        "Failed to parse the hosted frontend URL from {REMOTE_FRONTEND_URL_ENV_KEY}: {error}"
                    )
                })?;
                main_window.navigate(parsed_remote_url).map_err(|error| {
                    format!("Failed to navigate the desktop window to the hosted frontend: {error}")
                })?;

                return Ok(());
            }

            let mut sidecar_child = spawn_ui_sidecar(app)?;
            let sidecar_url = parse_sidecar_url("/setup")?;

            if let Err(error) = wait_for_sidecar(&mut sidecar_child, &sidecar_url) {
                terminate_child(&mut sidecar_child);
                return Err(error.into());
            }

            store_sidecar_child(app, sidecar_child)?;
            let main_window = app
                .get_webview_window("main")
                .ok_or("The main desktop window is missing from the Tauri configuration.")?;
            main_window.navigate(sidecar_url).map_err(|error| {
                format!("Failed to navigate the desktop window to the sidecar: {error}")
            })?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the Accounting AI Agent desktop shell");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_ui_sidecar(app_handle);
        }
    });
}

fn spawn_ui_sidecar(app: &App) -> Result<Child, String> {
    let runtime = resolve_sidecar_runtime(app)?;
    let server_dir = runtime
        .server_script
        .parent()
        .ok_or("The packaged desktop UI server path is missing a parent directory.")?;

    let mut command = Command::new(&runtime.node_binary);
    command
        .arg(&runtime.server_script)
        .current_dir(server_dir)
        .env("HOST", SIDECAR_HOST)
        .env("PORT", SIDECAR_PORT.to_string())
        .env("NEXT_TELEMETRY_DISABLED", "1")
        .env("NODE_ENV", runtime.node_env)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    for (key, value) in &runtime.sidecar_env {
        command.env(key, value);
    }

    command.spawn().map_err(|error| {
        format!(
            "Failed to start the Next.js desktop sidecar with {} and {}: {error}",
            runtime.node_binary.display(),
            runtime.server_script.display()
        )
    })
}

fn wait_for_sidecar(child: &mut Child, sidecar_url: &Url) -> Result<(), String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("Failed to create the loopback health client: {error}"))?;
    let deadline = Instant::now() + SIDECAR_STARTUP_TIMEOUT;

    while Instant::now() < deadline {
        if let Some(exit_status) = child
            .try_wait()
            .map_err(|error| format!("Unable to inspect the desktop sidecar process: {error}"))?
        {
            return Err(format!(
                "The desktop UI sidecar exited before startup completed with status {exit_status}. \
Run `pnpm --filter @accounting-ai-agent/desktop-ui build` and retry packaging."
            ));
        }

        match client.get(sidecar_url.clone()).send() {
            Ok(response) if response.status().is_success() => return Ok(()),
            Ok(_) | Err(_) => thread::sleep(SIDECAR_POLL_INTERVAL),
        }
    }

    Err(format!(
        "Timed out waiting for the desktop UI sidecar at {sidecar_url}. \
Run ./infra/scripts/package-desktop.sh again after confirming the standalone UI build exists."
    ))
}

fn store_sidecar_child(app: &App, child: Child) -> Result<(), String> {
    let state = app.state::<UiSidecarState>();
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "The desktop sidecar state lock is poisoned.".to_string())?;
    *guard = Some(child);
    Ok(())
}

fn stop_ui_sidecar(app_handle: &AppHandle) {
    let state = app_handle.state::<UiSidecarState>();
    let mut guard = match state.child.lock() {
        Ok(guard) => guard,
        Err(_) => return,
    };

    if let Some(mut child) = guard.take() {
        terminate_child(&mut child);
    }
}

fn terminate_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn parse_sidecar_url(path: &str) -> Result<Url, String> {
    Url::parse(&format!("http://{SIDECAR_HOST}:{SIDECAR_PORT}{path}"))
        .map_err(|error| format!("Failed to build the desktop sidecar URL: {error}"))
}

fn resolve_sidecar_runtime(app: &App) -> Result<SidecarRuntime, String> {
    if cfg!(debug_assertions) {
        return resolve_dev_runtime();
    }

    resolve_packaged_runtime(app)
}

fn resolve_dev_runtime() -> Result<SidecarRuntime, String> {
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .map_err(|error| {
            format!("Failed to resolve the repository root for the desktop shell: {error}")
        })?;
    ensure_dev_ui_build(&repo_root)?;
    let server_script =
        repo_root.join("apps/desktop-ui/.next/standalone/apps/desktop-ui/server.js");
    let repo_env_file = repo_root.join(".env");
    assert_readable_file(&server_script, "development desktop UI server")?;

    Ok(SidecarRuntime {
        node_binary: PathBuf::from("node"),
        node_env: "production",
        server_script,
        sidecar_env: resolve_sidecar_env(repo_env_file.is_file().then_some(repo_env_file))?,
    })
}

fn resolve_packaged_runtime(app: &App) -> Result<SidecarRuntime, String> {
    let resource_dir = app.path().resource_dir().map_err(|error| {
        format!("Failed to locate the packaged desktop-shell resources: {error}")
    })?;
    let node_binary = resource_dir
        .join("runtime")
        .join(platform_node_binary_name());
    let server_script = resource_dir.join("desktop-ui").join("server.js");

    assert_readable_file(&node_binary, "bundled Node runtime")?;
    assert_readable_file(&server_script, "bundled desktop UI server")?;

    Ok(SidecarRuntime {
        node_binary,
        node_env: "production",
        server_script,
        sidecar_env: resolve_sidecar_env(resolve_packaged_env_file_path(app)?)?,
    })
}

fn ensure_dev_ui_build(repo_root: &Path) -> Result<(), String> {
    let status = Command::new("pnpm")
        .args(DEV_UI_BUILD_COMMAND)
        .current_dir(repo_root)
        .status()
        .map_err(|error| {
            format!(
                "Failed to start the desktop UI build command before launching the debug shell: {error}"
            )
        })?;

    if status.success() {
        return Ok(());
    }

    Err(format!(
        "The desktop UI build failed before the debug shell could launch. \
Run `pnpm --filter @accounting-ai-agent/desktop-ui build` from {} and fix any reported errors.",
        repo_root.display()
    ))
}

fn resolve_packaged_env_file_path(app: &App) -> Result<Option<PathBuf>, String> {
    let app_config_dir = app.path().app_config_dir().map_err(|error| {
        format!("Failed to resolve the desktop-shell app config directory: {error}")
    })?;
    let env_file_path = app_config_dir.join(DESKTOP_SHELL_ENV_FILE_NAME);
    if env_file_path.is_file() {
        return Ok(Some(env_file_path));
    }

    Ok(None)
}

fn resolve_sidecar_env(env_file_path: Option<PathBuf>) -> Result<BTreeMap<String, String>, String> {
    let raw_values = resolve_raw_runtime_values(env_file_path)?;

    let mut sidecar_env = BTreeMap::new();
    sidecar_env.insert(
        "ACCOUNTING_AGENT_API_URL".to_string(),
        resolve_api_url(&raw_values),
    );
    sidecar_env.insert(
        "ACCOUNTING_AGENT_SESSION_COOKIE_NAME".to_string(),
        resolve_session_cookie_name(&raw_values),
    );
    sidecar_env.insert(
        "ACCOUNTING_AGENT_FRONTEND_MODE".to_string(),
        "desktop-local".to_string(),
    );
    sidecar_env.insert(
        "database_host".to_string(),
        get_runtime_value(&raw_values, "database_host", DEFAULT_DATABASE_HOST),
    );
    sidecar_env.insert(
        "database_port".to_string(),
        get_runtime_value(&raw_values, "database_port", DEFAULT_DATABASE_PORT),
    );
    sidecar_env.insert(
        "redis_broker_url".to_string(),
        get_runtime_value(&raw_values, "redis_broker_url", DEFAULT_REDIS_BROKER_URL),
    );
    sidecar_env.insert(
        "storage_endpoint".to_string(),
        get_runtime_value(&raw_values, "storage_endpoint", DEFAULT_STORAGE_ENDPOINT),
    );
    sidecar_env.insert(
        "storage_secure".to_string(),
        get_runtime_value(&raw_values, "storage_secure", DEFAULT_STORAGE_SECURE),
    );

    Ok(sidecar_env)
}

fn resolve_raw_runtime_values(
    env_file_path: Option<PathBuf>,
) -> Result<BTreeMap<String, String>, String> {
    let mut raw_values = BTreeMap::new();

    if let Some(path) = env_file_path {
        raw_values.extend(parse_env_file(&path)?);
    }

    for key in supported_runtime_env_keys() {
        if let Ok(value) = env::var(key) {
            raw_values.insert(key.to_string(), value);
        }
    }

    Ok(raw_values)
}

fn supported_runtime_env_keys() -> [&'static str; 13] {
    [
        "ACCOUNTING_AGENT_DESKTOP_REMOTE_URL",
        "ACCOUNTING_AGENT_FRONTEND_MODE",
        "ACCOUNTING_AGENT_API_URL",
        "ACCOUNTING_AGENT_SESSION_COOKIE_NAME",
        "api_host",
        "api_port",
        "database_host",
        "database_port",
        "redis_broker_url",
        "runtime_api_base_path",
        "security_session_cookie_name",
        "storage_endpoint",
        "storage_secure",
    ]
}

fn resolve_remote_frontend_url(app: &App) -> Result<Option<String>, String> {
    let raw_values = if cfg!(debug_assertions) {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .map_err(|error| {
                format!(
                    "Failed to resolve the repository root for the desktop shell: {error}"
                )
            })?;
        let repo_env_file = repo_root.join(".env");
        resolve_raw_runtime_values(repo_env_file.is_file().then_some(repo_env_file))?
    } else {
        resolve_raw_runtime_values(resolve_packaged_env_file_path(app)?)?
    };

    Ok(raw_values
        .get(REMOTE_FRONTEND_URL_ENV_KEY)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty()))
}

fn resolve_api_url(raw_values: &BTreeMap<String, String>) -> String {
    if let Some(explicit_api_url) = raw_values.get("ACCOUNTING_AGENT_API_URL") {
        return explicit_api_url.trim().trim_end_matches('/').to_string();
    }

    let api_host = get_runtime_value(raw_values, "api_host", DEFAULT_API_HOST);
    let api_port = get_runtime_value(raw_values, "api_port", DEFAULT_API_PORT);
    let api_base_path = normalize_api_base_path(
        raw_values
            .get("runtime_api_base_path")
            .map(String::as_str)
            .unwrap_or(DEFAULT_API_BASE_PATH),
    );

    format!("http://{api_host}:{api_port}{api_base_path}")
}

fn resolve_session_cookie_name(raw_values: &BTreeMap<String, String>) -> String {
    if let Some(cookie_name) = raw_values.get("ACCOUNTING_AGENT_SESSION_COOKIE_NAME") {
        return cookie_name.trim().to_string();
    }

    raw_values
        .get("security_session_cookie_name")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_SESSION_COOKIE_NAME.to_string())
}

fn get_runtime_value(raw_values: &BTreeMap<String, String>, key: &str, fallback: &str) -> String {
    raw_values
        .get(key)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn normalize_api_base_path(value: &str) -> String {
    let trimmed_value = value.trim();
    if trimmed_value.is_empty() || trimmed_value == "/" {
        return String::new();
    }

    if trimmed_value.starts_with('/') {
        trimmed_value.trim_end_matches('/').to_string()
    } else {
        format!("/{}", trimmed_value.trim_end_matches('/'))
    }
}

fn parse_env_file(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let contents = fs::read_to_string(path).map_err(|error| {
        format!(
            "Failed to read the desktop-shell runtime configuration file at {}: {error}",
            path.display()
        )
    })?;
    let mut parsed_values = BTreeMap::new();

    for (index, line) in contents.lines().enumerate() {
        let trimmed_line = line.trim();
        if trimmed_line.is_empty() || trimmed_line.starts_with('#') {
            continue;
        }

        let normalized_line = trimmed_line.strip_prefix("export ").unwrap_or(trimmed_line);
        let (raw_key, raw_value) = normalized_line.split_once('=').ok_or_else(|| {
            format!(
                "Invalid runtime configuration line {} in {}. \
Expected KEY=VALUE syntax.",
                index + 1,
                path.display()
            )
        })?;

        let key = raw_key.trim();
        if key.is_empty() {
            return Err(format!(
                "Invalid runtime configuration line {} in {}. \
The key cannot be empty.",
                index + 1,
                path.display()
            ));
        }

        parsed_values.insert(
            key.to_string(),
            unquote_env_value(raw_value.trim()).to_string(),
        );
    }

    Ok(parsed_values)
}

fn unquote_env_value(value: &str) -> &str {
    if value.len() >= 2 {
        let first_character = value.chars().next();
        let last_character = value.chars().last();
        if matches!(
            (first_character, last_character),
            (Some('"'), Some('"')) | (Some('\''), Some('\''))
        ) {
            return &value[1..value.len() - 1];
        }
    }

    value
}

fn assert_readable_file(path: &Path, label: &str) -> Result<(), String> {
    if path.is_file() {
        return Ok(());
    }

    Err(format!(
        "The {label} is missing at {}. Re-run ./infra/scripts/package-desktop.sh to rebuild the installer assets.",
        path.display()
    ))
}

fn platform_node_binary_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "node.exe"
    } else {
        "node"
    }
}

struct SidecarRuntime {
    node_binary: PathBuf,
    node_env: &'static str,
    server_script: PathBuf,
    sidecar_env: BTreeMap<String, String>,
}
