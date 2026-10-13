//! Embeds assets/kopy.ico and Windows version info into kopy.exe.

fn main() {
    println!("cargo:rerun-if-changed=assets/kopy.ico");
    println!("cargo:rerun-if-changed=build.rs");

    // build.rs runs on the host; check the *target* so cross-builds behave.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let mut res = winresource::WindowsResource::new();
    res.set_icon("assets/kopy.ico");
    res.set("FileDescription", "kopy - robocopy-style file copier");
    res.set("ProductName", "kopy");
    // An icon-less exe still works, so don't fail the build over a missing SDK.
    if let Err(e) = res.compile() {
        println!("cargo:warning=could not embed icon (is the Windows SDK installed?): {e}");
    }
}
