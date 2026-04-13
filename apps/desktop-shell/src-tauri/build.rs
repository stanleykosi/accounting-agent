// Purpose: Run the canonical Tauri build script so the desktop shell can bundle its launcher assets.
// Scope: Tauri build-time metadata generation only.
// Dependencies: tauri-build declared in Cargo.toml.

fn main() {
    tauri_build::build()
}
