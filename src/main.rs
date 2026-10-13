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
    match engine::run(args) {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            eprintln!("kopy: fatal: {e:#}");
            ExitCode::from(16)
        }
    }
}
