//! Code Visualizer desktop: one tab per repository, each showing the graph
//! page of that repository's `cviz graph` server.
//!
//! The servers are the ones the CLI runs (one per repository, its own port,
//! recorded in ~/.code-visualizer/servers.json). Opening a tab runs
//! `cviz graph --watch <repo> --no-browser`: it starts the repository's server,
//! or, when one already runs (started from a terminal or an IDE), prints its
//! URL and exits. The app stops only the servers it started.
//!
//! IDEs reach the app through a loopback endpoint (see `ide_link`): it brings
//! the window forward on the repository's tab, centred on the artifact.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
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
    /// A server already running with another cviz version: (its, the app's).
    other_version: Option<(String, String)>,
}

/// What the "already runs" / "graph server on" line of `cviz graph` says.
#[derive(Debug, PartialEq)]
struct Found {
    url: String,
    already_running: bool,
    other_version: Option<(String, String)>,
}

fn parse_server_line(line: &str) -> Option<Found> {
    if !(line.contains("already runs") || line.contains("graph server on")) {
        return None;
    }
    let url = Regex::new(r"https?://[^\s/]+:\d+/graph").unwrap();
    let versions = Regex::new(r"\(cviz ([^,\s]+), not ([^)\s]+)\)").unwrap();
    let found = url.find(line)?;
    Some(Found {
        url: found.as_str().to_string(),
        already_running: line.contains("already runs"),
        other_version: versions.captures(line).map(|c| (c[1].to_string(), c[2].to_string())),
    })
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
fn relay<E: Fn(LogLine) + Send + 'static>(emit: E, path: String, stream: impl Read + Send + 'static, url_tx: mpsc::Sender<Found>) {
    thread::spawn(move || {
        let ansi = Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").unwrap();
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let line = ansi.replace_all(&line, "").to_string();
            if let Some(found) = parse_server_line(&line) {
                let _ = url_tx.send(found);
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
    restart: bool,
) -> Result<RepoServer, String> {
    let program = cviz_program();
    let mut cmd = Command::new(&program);
    if let Some(dir) = grammars {
        cmd.env("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", dir);
    }
    cmd.args(["graph", "--watch", path, "--no-browser"]);
    if restart {
        cmd.arg("--restart");  // the server of another version is stopped first
    }
    cmd
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
    let Found { url, already_running, other_version } = loop {
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
    Ok(RepoServer { url, started, other_version })
}

/// The graph page of *path*, starting its server when none runs.
#[tauri::command]
async fn open_repo(app: AppHandle, path: String, restart: Option<bool>) -> Result<RepoServer, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let emitter = app.clone();
        let emit = move |line: LogLine| {
            let _ = emitter.emit("repo-log", line);
        };
        let grammars = grammars_dir(&app);
        start_server(&app.state::<Servers>(), emit, &path, grammars, restart.unwrap_or(false))
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

/// Brings the window forward (from a graph page that an IDE recentred).
#[tauri::command]
fn focus_window(app: AppHandle) {
    reveal(&app);
}

fn reveal(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// The IDE side of the app: `cviz-open` and the IDE extensions find the app in
/// ~/.code-visualizer/desktop.json (its URL and a token) and send
/// `POST /open {"path", "line", "repo"}`. The window comes forward and the
/// shell (app.js, "ide-open") opens or activates the repository's tab,
/// centred on the artifact. Loopback only; the token, readable by the user
/// alone, keeps web pages out (they could post to a local port).
mod ide_link {
    use super::*;

    #[derive(serde::Deserialize, serde::Serialize, Clone, Debug, PartialEq)]
    pub struct OpenRequest {
        pub path: String,
        #[serde(default)]
        pub line: Option<u64>,
        #[serde(default)]
        pub repo: Option<String>,
    }

    /// ~/.code-visualizer/desktop.json, or $CVIZ_DESKTOP_FILE.
    pub fn link_file() -> Option<PathBuf> {
        if let Ok(custom) = std::env::var("CVIZ_DESKTOP_FILE") {
            return Some(PathBuf::from(custom));
        }
        let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
        Some(PathBuf::from(home).join(".code-visualizer").join("desktop.json"))
    }

    fn new_token() -> String {
        use std::collections::hash_map::RandomState;
        use std::hash::{BuildHasher, Hasher};
        // RandomState is seeded from the OS's random source.
        (0..4)
            .map(|i| {
                let mut h = RandomState::new().build_hasher();
                h.write_u64(i);
                format!("{:016x}", h.finish())
            })
            .collect()
    }

    /// Reads one request: the open request of a valid `POST /open`, else the
    /// HTTP status to answer.
    pub fn read_request(stream: &mut impl Read, token: &str) -> Result<OpenRequest, (u16, &'static str)> {
        let mut reader = BufReader::new(stream);
        let mut first = String::new();
        reader.read_line(&mut first).map_err(|_| (400, "bad request"))?;
        let mut parts = first.split_whitespace();
        let (method, target) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
        let (mut length, mut given) = (0usize, String::new());
        loop {
            let mut header = String::new();
            if reader.read_line(&mut header).map_err(|_| (400, "bad request"))? == 0 {
                break;
            }
            let header = header.trim_end();
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':') {
                match name.trim().to_ascii_lowercase().as_str() {
                    "content-length" => length = value.trim().parse().unwrap_or(0),
                    "x-cviz-token" => given = value.trim().to_string(),
                    _ => {}
                }
            }
        }
        if method != "POST" || target != "/open" {
            return Err((404, "not found"));
        }
        if token.is_empty() || given != token {
            return Err((403, "forbidden"));
        }
        if length > 64 * 1024 {
            return Err((413, "too large"));
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).map_err(|_| (400, "bad request"))?;
        let open: OpenRequest = serde_json::from_slice(&body).map_err(|_| (400, "invalid JSON"))?;
        if open.path.is_empty() {
            return Err((400, "'path' is required"));
        }
        Ok(open)
    }

    fn respond(stream: &mut TcpStream, status: u16, body: &str) {
        let reason = match status { 200 => "OK", 403 => "Forbidden", 404 => "Not Found", 413 => "Payload Too Large", _ => "Bad Request" };
        let _ = write!(
            stream,
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
    }

    /// Listens on a free loopback port and records it in desktop.json.
    pub fn start<F: Fn(OpenRequest) + Send + 'static>(on_open: F) -> Option<PathBuf> {
        let listener = TcpListener::bind("127.0.0.1:0").ok()?;
        let port = listener.local_addr().ok()?.port();
        let token = new_token();
        let file = link_file()?;
        if let Some(dir) = file.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let record = serde_json::json!({
            "url": format!("http://127.0.0.1:{port}"),
            "token": token,
            "pid": std::process::id(),
        });
        std::fs::write(&file, record.to_string()).ok()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600));
        }
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut stream = stream;
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                match read_request(&mut stream, &token) {
                    Ok(open) => {
                        respond(&mut stream, 200, "{\"ok\":true}");
                        on_open(open);
                    }
                    Err((status, why)) => respond(&mut stream, status, &format!("{{\"error\":\"{why}\"}}")),
                }
            }
        });
        Some(file)
    }

    /// Removes desktop.json if it is still this app's (another app may have
    /// replaced it since).
    pub fn stop(file: &std::path::Path) {
        let ours = std::fs::read_to_string(file)
            .ok()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
            .and_then(|v| v.get("pid").and_then(|p| p.as_u64()))
            == Some(std::process::id() as u64);
        if ours {
            let _ = std::fs::remove_file(file);
        }
    }
}

/// desktop.json of this app, removed at exit.
struct LinkFile(Mutex<Option<PathBuf>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Servers::default())
        .manage(LinkFile(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let file = ide_link::start(move |open| {
                reveal(&handle);
                let _ = handle.emit("ide-open", open);
            });
            *app.state::<LinkFile>().0.lock().unwrap() = file;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![open_repo, close_repo, open_url, focus_window])
        .build(tauri::generate_context!())
        .expect("error while building the app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(file) = app.state::<LinkFile>().0.lock().unwrap().take() {
                    ide_link::stop(&file);
                }
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

    #[test]
    fn the_server_line_gives_the_url_and_another_version() {
        let started = parse_server_line("Dependency graph server on http://localhost:9324/graph").unwrap();
        assert_eq!(started, Found { url: "http://localhost:9324/graph".into(), already_running: false, other_version: None });
        let same = parse_server_line("The graph server of this repository already runs: http://localhost:9325/graph").unwrap();
        assert!(same.already_running && same.other_version.is_none());
        let other = parse_server_line(
            "The graph server of this repository already runs: http://localhost:9325/graph?path=%2Fr%2Fa.py (cviz 1.9.0, not 0.9.9)",
        ).unwrap();
        assert_eq!(other.url, "http://localhost:9325/graph");
        assert_eq!(other.other_version, Some(("1.9.0".into(), "0.9.9".into())));
        assert!(parse_server_line("Indexing: parse [3/10]").is_none());
    }

    /// Needs a `cviz` binary (CVIZ_DESKTOP_CVIZ) and a repository to open
    /// (CVIZ_DESKTOP_TEST_REPO); skipped otherwise.
    #[test]
    fn starts_reuses_and_stops_a_server() {
        let Ok(repo) = std::env::var("CVIZ_DESKTOP_TEST_REPO") else { return };
        let servers = Servers::default();
        let lines = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
        let sink = lines.clone();
        let emit = move |l: LogLine| sink.lock().unwrap().push(l.line);

        let first = start_server(&servers, emit.clone(), &repo, None, false).expect("server started");
        assert!(first.started && answers(&first.url), "{:?}", lines.lock().unwrap());

        // A second app (or the same one after a restart) reuses it.
        let other = Servers::default();
        let second = start_server(&other, emit, &repo, None, false).expect("server found");
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

#[cfg(test)]
mod ide_link_tests {
    use super::ide_link::*;

    fn request(head: &str, body: &str) -> String {
        format!("{head}\r\nContent-Length: {}\r\n\r\n{body}", body.len())
    }

    #[test]
    fn reads_a_valid_open_request() {
        let raw = request("POST /open HTTP/1.1\r\nHost: x\r\nX-CViz-Token: abc", r#"{"path":"/r/a.py","line":12,"repo":"/r"}"#);
        let open = read_request(&mut raw.as_bytes(), "abc").unwrap();
        assert_eq!(open, OpenRequest { path: "/r/a.py".into(), line: Some(12), repo: Some("/r".into()) });
    }

    #[test]
    fn refuses_a_missing_or_wrong_token_and_other_routes() {
        let body = r#"{"path":"/r/a.py"}"#;
        let no_token = request("POST /open HTTP/1.1", body);
        assert_eq!(read_request(&mut no_token.as_bytes(), "abc").unwrap_err().0, 403);
        let wrong = request("POST /open HTTP/1.1\r\nx-cviz-token: nope", body);
        assert_eq!(read_request(&mut wrong.as_bytes(), "abc").unwrap_err().0, 403);
        let route = request("GET /open HTTP/1.1\r\nX-CViz-Token: abc", "");
        assert_eq!(read_request(&mut route.as_bytes(), "abc").unwrap_err().0, 404);
        let empty = request("POST /open HTTP/1.1\r\nX-CViz-Token: abc", r#"{"path":""}"#);
        assert_eq!(read_request(&mut empty.as_bytes(), "abc").unwrap_err().0, 400);
    }

    #[test]
    fn serves_requests_and_records_then_removes_its_file() {
        let file = std::env::temp_dir().join(format!("cviz-desktop-{}.json", std::process::id()));
        std::env::set_var("CVIZ_DESKTOP_FILE", &file);
        let (tx, rx) = std::sync::mpsc::channel();
        let written = start(move |open| { let _ = tx.send(open); }).unwrap();
        assert_eq!(written, file);
        let record: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
        let (url, token) = (record["url"].as_str().unwrap(), record["token"].as_str().unwrap());
        assert!(token.len() >= 32);

        let mut stream = std::net::TcpStream::connect(url.trim_start_matches("http://")).unwrap();
        use std::io::{Read, Write};
        let body = r#"{"path":"/r/a.py","line":3}"#;
        write!(stream, "POST /open HTTP/1.1\r\nX-CViz-Token: {token}\r\nContent-Length: {}\r\n\r\n{body}", body.len()).unwrap();
        let mut answer = String::new();
        stream.read_to_string(&mut answer).unwrap();
        assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
        let open = rx.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        assert_eq!((open.path.as_str(), open.line), ("/r/a.py", Some(3)));

        stop(&file);
        assert!(!file.exists());
    }
}
