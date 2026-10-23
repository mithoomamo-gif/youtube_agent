// Prevents an additional console window on Windows in release builds.
// Do not remove this line.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    youtube_agent_lib::run()
}
