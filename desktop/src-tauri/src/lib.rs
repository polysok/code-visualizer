//! Code Visualizer desktop: one tab per repository, each showing the graph
//! page of that repository's `cviz graph` server.
//!
//! The servers are the ones the CLI runs (one per repository, its own port,
//! recorded in ~/.code-visualizer/servers.json). Opening a tab runs
//! `cviz graph --watch <repo> --no-browser`: it starts the repository's server,
//! or, when one already runs (started from a terminal or an IDE), prints its
//! URL and exits. The app stops only the servers it started.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use regex::Regex;
use tauri::{AppHandle, Emitter, Manager, RunEvent};
use tauri_plugin_opener::OpenerExt;

/// Servers this app started, by repository path.
#[derive(Default)]
struct Servers(Mutex<HashMap<String, Child>>);

#[derive(serde::Serialize, Clone)]
struct RepoServer {
    url: String,
    /// False when the server was already running (the app will not stop it).
    started: bool,
}

#[derive(serde::Serialize, Clone)]
struct LogLine {
    path: String,
    line: String,
}

/// The tree-sitter grammars cviz uses (tree-sitter-language-pack), shipped
/// with the app so that a first index needs no network. They are copied to
/// the app's cache folder, which cviz may add other grammars to.
fn grammars_dir(app: &AppHandle) -> Option<PathBuf> {
    static READY: std::sync::OnceLock<Option<PathBuf>> = std::sync::OnceLock::new();
    READY
        .get_or_init(|| {
            let cache = app.path().app_cache_dir().ok()?.join("grammars");
            if let Ok(bundled) = app.path().resource_dir().map(|d| d.join("grammars")) {
                if bundled.is_dir() {
                    copy_missing(&bundled, &cache);
                }
            }
            std::fs::create_dir_all(&cache).ok()?;
            Some(cache)
        })
        .clone()
}

/// Copies the files of *from* that *to* does not have yet (best effort).
fn copy_missing(from: &std::path::Path, to: &std::path::Path) {
    let Ok(entries) = std::fs::read_dir(from) else { return };
    let _ = std::fs::create_dir_all(to);
    for entry in entries.flatten() {
        let target = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy_missing(&entry.path(), &target);
        } else if !target.exists() {
            let _ = std::fs::copy(entry.path(), &target);
        }
    }
}

/// How long the first start of a repository may take: it indexes it first.
const START_TIMEOUT: Duration = Duration::from_secs(30 * 60);

/// The `cviz` program: $CVIZ_DESKTOP_CVIZ, else the one bundled next to the
/// app's executable (Tauri's externalBin), else `cviz` on the PATH.
fn cviz_program() -> PathBuf {
    if let Ok(custom) = std::env::var("CVIZ_DESKTOP_CVIZ") {
        return PathBuf::from(custom);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let bundled = dir.join(if cfg!(windows) { "cviz.exe" } else { "cviz" });
            if bundled.is_file() {
                return bundled;
            }
        }
    }
    PathBuf::from("cviz")
}

fn stop(mut child: Child) {
    #[cfg(unix)]
    {
        // The PyInstaller binary runs the program in a child process: stop
        // the whole process group.
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Relays a stream of the server's output to the tab; reports the URL line.
/// The URL is sent with whether the server was already running.
fn relay<E: Fn(LogLine) + Send + 'static>(emit: E, path: String, stream: impl Read + Send + 'static, url_tx: mpsc::Sender<(String, bool)>) {
    thread::spawn(move || {
        let ansi = Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").unwrap();
        let url = Regex::new(r"https?://[^\s/]+:\d+/graph").unwrap();
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let line = ansi.replace_all(&line, "").to_string();
            if line.contains("already runs") || line.contains("graph server on") {
                if let Some(found) = url.find(&line) {
                    let _ = url_tx.send((found.as_str().to_string(), line.contains("already runs")));
                }
            }
            emit(LogLine { path: path.clone(), line });
        }
    });
}

fn answers(url: &str) -> bool {
    let re = Regex::new(r"https?://([^\s/:]+):(\d+)").unwrap();
    let Some(caps) = re.captures(url) else { return false };
    let host = if &caps[1] == "localhost" { "127.0.0.1" } else { &caps[1] };
    format!("{}:{}", host, &caps[2])
        .parse()
        .map(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok())
        .unwrap_or(false)
}

fn start_server<E: Fn(LogLine) + Clone + Send + 'static>(
    servers: &Servers,
    emit: E,
    path: &str,
    grammars: Option<PathBuf>,
) -> Result<RepoServer, String> {
    let program = cviz_program();
    let mut cmd = Command::new(&program);
    if let Some(dir) = grammars {
        cmd.env("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", dir);
    }
    cmd.args(["graph", "--watch", path, "--no-browser"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUNBUFFERED", "1")
        .env("NO_COLOR", "1")
        .env("COLUMNS", "200");
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Could not run {}: {e}", program.display()))?;
    let (tx, rx) = mpsc::channel();
    relay(emit.clone(), path.to_string(), child.stdout.take().unwrap(), tx.clone());
    relay(emit, path.to_string(), child.stderr.take().unwrap(), tx);

    servers.0.lock().unwrap().insert(path.to_string(), child);
    let started_at = Instant::now();
    let (url, already_running) = loop {
        match rx.recv_timeout(Duration::from_millis(300)) {
            Ok(found) => break found,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                // Both streams closed without a URL: the program has ended.
                let child = servers.0.lock().unwrap().remove(path);
                let code = child.and_then(|mut c| c.wait().ok()).and_then(|s| s.code());
                let how = code.map(|c| format!(" (exit code {c})")).unwrap_or_default();
                return Err(format!("cviz graph stopped{how} without starting a server: its output is above."));
            }
        }
        if started_at.elapsed() > START_TIMEOUT {
            if let Some(child) = servers.0.lock().unwrap().remove(path) {
                stop(child);
            }
            return Err("cviz graph did not start within 30 minutes".into());
        }
    };

    // "already runs": that program exits by itself, the server is not ours.
    if already_running {
        if let Some(child) = servers.0.lock().unwrap().remove(path) {
            thread::spawn(move || {
                let mut child = child;
                let _ = child.wait();
            });
        }
    }
    let deadline = Instant::now() + Duration::from_secs(60);
    while !answers(&url) {
        if Instant::now() > deadline {
            return Err(format!("{url} does not answer"));
        }
        thread::sleep(Duration::from_millis(200));
    }
    let started = !already_running;
    Ok(RepoServer { url, started })
}

/// The graph page of *path*, starting its server when none runs.
#[tauri::command]
async fn open_repo(app: AppHandle, path: String) -> Result<RepoServer, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let emitter = app.clone();
        let emit = move |line: LogLine| {
            let _ = emitter.emit("repo-log", line);
        };
        let grammars = grammars_dir(&app);
        start_server(&app.state::<Servers>(), emit, &path, grammars)
    })
        .await
        .map_err(|e| e.to_string())?
}

/// Stops the server of *path* if this app started it.
#[tauri::command]
fn close_repo(app: AppHandle, path: String) {
    let child = app.state::<Servers>().0.lock().unwrap().remove(&path);
    if let Some(child) = child {
        thread::spawn(move || stop(child));
    }
}

/// Opens an IDE link (vscode://, cursor://, zed://, jetbrains://...) with the
/// system: a tab cannot follow it itself.
#[tauri::command]
fn open_url(app: AppHandle, url: String) -> Result<(), String> {
    let allowed = ["vscode:", "vscode-insiders:", "cursor:", "zed:", "jetbrains:", "idea:", "rider:", "http:", "https:"];
    if !allowed.iter().any(|scheme| url.starts_with(scheme)) {
        return Err(format!("not opened: {url}"));
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Servers::default())
        .invoke_handler(tauri::generate_handler![open_repo, close_repo, open_url])
        .build(tauri::generate_context!())
        .expect("error while building the app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let children: Vec<Child> = app.state::<Servers>().0.lock().unwrap().drain().map(|(_, c)| c).collect();
                for child in children {
                    stop(child);
                }
            }
        });
}


#[cfg(test)]
mod tests {
    use super::*;

    /// Needs a `cviz` binary (CVIZ_DESKTOP_CVIZ) and a repository to open
    /// (CVIZ_DESKTOP_TEST_REPO); skipped otherwise.
    #[test]
    fn starts_reuses_and_stops_a_server() {
        let Ok(repo) = std::env::var("CVIZ_DESKTOP_TEST_REPO") else { return };
        let servers = Servers::default();
        let lines = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = lines.clone();
        let emit = move |l: LogLine| sink.lock().unwrap().push(l.line);

        let first = start_server(&servers, emit.clone(), &repo, None).expect("server started");
        assert!(first.started && answers(&first.url), "{:?}", lines.lock().unwrap());

        // A second app (or the same one after a restart) reuses it.
        let other = Servers::default();
        let second = start_server(&other, emit, &repo, None).expect("server found");
        assert_eq!(second.url, first.url);
        assert!(!second.started);
        assert!(other.0.lock().unwrap().is_empty());

        let child = servers.0.lock().unwrap().remove(&repo).unwrap();
        stop(child);
        let deadline = Instant::now() + Duration::from_secs(10);
        while answers(&first.url) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(200));
        }
        assert!(!answers(&first.url), "the server still answers after stop");
    }
}

#[cfg(test)]
mod grammar_tests {
    use super::*;

    #[test]
    fn copies_only_the_missing_grammars() {
        let base = std::env::temp_dir().join(format!("cviz-grammars-{}", std::process::id()));
        let (from, to) = (base.join("from/pack/v1/libs"), base.join("to"));
        std::fs::create_dir_all(&from).unwrap();
        std::fs::write(from.join("libtree_sitter_c.dylib"), b"bundled").unwrap();
        std::fs::write(from.join("libtree_sitter_go.dylib"), b"bundled").unwrap();
        std::fs::create_dir_all(to.join("pack/v1/libs")).unwrap();
        std::fs::write(to.join("pack/v1/libs/libtree_sitter_go.dylib"), b"already there").unwrap();

        copy_missing(&base.join("from"), &to);

        let read = |name: &str| std::fs::read(to.join("pack/v1/libs").join(name)).unwrap();
        assert_eq!(read("libtree_sitter_c.dylib"), b"bundled");
        assert_eq!(read("libtree_sitter_go.dylib"), b"already there");
        let _ = std::fs::remove_dir_all(&base);
    }
}
