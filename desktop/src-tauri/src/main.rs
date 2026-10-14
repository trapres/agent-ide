use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager};

struct Backend {
    input: Mutex<ChildStdin>,
    child: Arc<Mutex<Child>>,
    runtime: Value,
    closing: AtomicBool,
}

#[tauri::command]
fn desktop_send(message: Value, backend: tauri::State<Backend>) -> Result<(), String> {
    let mut input = backend.input.lock().map_err(|e| e.to_string())?;
    serde_json::to_writer(&mut *input, &message).map_err(|e| e.to_string())?;
    input.write_all(b"\n").map_err(|e| e.to_string())?;
    input.flush().map_err(|e| e.to_string())
}

#[tauri::command]
fn desktop_defaults(backend: tauri::State<Backend>) -> Value {
    backend.runtime.clone()
}

#[tauri::command]
async fn choose_directory() -> Option<String> {
    rfd::AsyncFileDialog::new()
        .pick_folder()
        .await
        .map(|f| f.path().to_string_lossy().into_owned())
}

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("Labradour checkout missing");
    let runtime: Value = serde_json::from_slice(
        &std::fs::read(root.join(".labradour-spike/desktop-runtime.json"))
            .expect("Run npm run build from desktop first"),
    )
    .expect("Invalid desktop runtime config");
    let python = std::env::var_os("LABRADOUR_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join(".venv/bin/python"));
    let mut child = Command::new(python)
        .args(["-u", "-m", "labradour.desktop"])
        .env("PATH", runtime["path"].as_str().unwrap_or("/usr/bin:/bin"))
        .current_dir(&root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("Cannot start Labradour Python backend; install requirements.txt in .venv");
    let input = child.stdin.take().unwrap();
    let output = child.stdout.take().unwrap();
    let process = Arc::new(Mutex::new(child));
    let backend = Backend {
        input: Mutex::new(input),
        child: process,
        runtime,
        closing: AtomicBool::new(false),
    };
    tauri::Builder::default()
        .manage(backend)
        .setup(move |app| {
            let handle = app.handle().clone();
            thread::spawn(move || {
                for line in BufReader::new(output).lines() {
                    match line {
                        Ok(line) => match serde_json::from_str::<Value>(&line) {
                            Ok(value) => { let _ = handle.emit("desktop-message", value); }
                            Err(_) => { let _ = handle.emit("desktop-message", json!({"event":"error","payload":{"message":"Invalid backend response"}})); }
                        },
                        Err(_) => break,
                    }
                }
                let _ = handle.emit("desktop-message", json!({"event":"backend-exit","payload":{}}));
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![desktop_send, desktop_defaults, choose_directory])
        .build(tauri::generate_context!())
        .expect("Cannot build Labradour desktop")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                let backend = app.state::<Backend>();
                if backend.closing.swap(true, Ordering::SeqCst) {
                    return;
                }
                api.prevent_exit();
                if let Ok(mut input) = backend.input.lock() {
                    let _ = input.write_all(b"{\"id\":0,\"method\":\"shutdown\"}\n");
                    let _ = input.flush();
                }
                let process = backend.child.clone();
                let handle = app.clone();
                thread::spawn(move || {
                    let deadline = Instant::now() + Duration::from_secs(40);
                    loop {
                        if let Ok(mut child) = process.lock() {
                            if child.try_wait().ok().flatten().is_some() { break; }
                            if Instant::now() > deadline {
                                // Python normally closes its PTY group and recorder before exiting.
                                let _ = child.kill();
                                let _ = child.wait();
                                break;
                            }
                        }
                        thread::sleep(Duration::from_millis(40));
                    }
                    handle.exit(0);
                });
            }
        });
}
