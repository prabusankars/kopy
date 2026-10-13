//! kopy - a robocopy-style file copier tuned for network share -> USB secure drive.

mod cli;
mod copy;
mod ctx;
mod engine;
mod error;
mod extras;
mod filters;
mod report;
mod scan;
mod util;

use clap::Parser;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args = cli::Args::parse();
    // Scripts check robocopy-style codes, so a panic must still exit 16, not 101.
    match std::panic::catch_unwind(|| engine::run(args)) {
        Ok(Ok(code)) => ExitCode::from(code),
        Ok(Err(e)) => {
            eprintln!("kopy: fatal: {e:#}");
            ExitCode::from(16)
        }
        Err(payload) => {
            eprintln!("kopy: fatal: internal error: {}", copy::panic_message(&*payload));
            ExitCode::from(16)
        }
    }
}
