import os
import sys
import re
import argparse
from pathlib import Path

# Common vendors found in alsa-ucm-conf/ucm2
VENDORS = [
    "AMD", "Allwinner", "Amlogic", "HDA", "IO-Boards", "Intel",
    "MediaTek", "NXP", "OMAP", "Qualcomm", "Rockchip", "Samsung",
    "Tegra", "USB-Audio", "sof-soundwire"
]

def sanitize_name(name):
    """Sanitize file paths to create valid Android.bp module names."""
    return name.replace("/", "_").replace(".", "_").replace("-", "_").replace("+", "_").replace(",", "_").replace(" ", "_")

def build_codec_usage(root_dir):
    """Scans all vendor configurations to see which vendor uses which codec."""
    codec_usage = {}
    for vendor in VENDORS:
        vendor_dir = root_dir / vendor
        if not vendor_dir.exists():
            continue
        for conf_file in vendor_dir.rglob("*.conf"):
            try:
                with open(conf_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Find occurrences of /codecs/<codec_name>/
                    matches = re.findall(r'/codecs/([^/]+)/', content)
                    for match in set(matches):
                        # Filter out variable placeholders like ${var:MicCodecFile}
                        if not match.startswith("${"):
                            codec_usage.setdefault(match, set()).add(vendor.lower())
            except Exception:
                pass
    return codec_usage

def main():
    parser = argparse.ArgumentParser(description="Generate Android.bp for alsa-ucm-conf")
    parser.add_argument("--legacy", action="store_true", help="Generate Android.mk for symlinks instead of install_symlink in Android.bp")
    parser.add_argument("--soong-namespace", action="store_true", help="Add an empty soong_namespace {} to the main Android.bp")
    args = parser.parse_args()

    root_dir = Path("ucm2")
    if not root_dir.exists() or not root_dir.is_dir():
        print("Error: ucm2 directory not found. Please run this script from the root of alsa-ucm-conf.")
        sys.exit(1)

    # 1. Build dynamic codec usage map
    codec_usage = build_codec_usage(root_dir)

    # Store tuples of (type, path, target)
    modules = []
    skipped = []

    # 2. Walk through the ucm2 directory
    for path in root_dir.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue

        rel_path = path.relative_to(root_dir)
        
        # Skip files/symlinks with spaces in the name
        if " " in str(rel_path):
            skipped.append(str(rel_path))
            continue
            
        parts = rel_path.parts
        first_dir = parts[0]
        
        # Determine the package name for the file/symlink
        pkg = "common"
        if first_dir in VENDORS:
            pkg = first_dir.lower()
        elif first_dir == "codecs" and len(parts) > 1:
            # Decommonize logic:
            codec_name = parts[1]
            usage = codec_usage.get(codec_name, set())
            if len(usage) == 1:
                # If only used by exactly one vendor, move it to that vendor's package
                pkg = list(usage)[0]
        else:
            # For anything else (e.g. conf.d, module), if it's a symlink, resolve it
            # to see if it points to a vendor directory (or contains symlinks to one).
            try:
                if path.is_symlink():
                    target_path = path.resolve()
                    if target_path.is_dir():
                        for child in target_path.rglob("*"):
                            if child.is_symlink():
                                child_target = child.resolve()
                                for part in child_target.parts:
                                    if part in VENDORS:
                                        pkg = part.lower()
                                        break
                            if pkg != "common":
                                break
                    else:
                        for part in target_path.parts:
                            if part in VENDORS:
                                pkg = part.lower()
                                break
            except Exception:
                pass
        
        is_symlink = path.is_symlink()
        target = os.readlink(path) if is_symlink else None
        
        modules.append({
            "pkg": pkg,
            "type": "symlink" if is_symlink else "file",
            "path": str(path),
            "rel_path": str(rel_path),
            "target": target
        })

    # Group modules by pkg
    pkg_map = {}
    for mod in modules:
        pkg_map.setdefault(mod["pkg"], []).append(mod)

    # 3. Generate the individual Android_<pkg>.bp files and optionally Android_<pkg>.mk
    generated_bps = []
    generated_mks = []
    
    for pkg, mods in pkg_map.items():
        bp_name = f"Android_{pkg}.bp"
        generated_bps.append(bp_name)
        
        # If legacy mode is enabled and this package has symlinks, generate an Android_<pkg>.mk file
        has_symlink = any(mod["type"] == "symlink" for mod in mods)
        if args.legacy and has_symlink:
            mk_name = f"Android_{pkg}.mk"
            generated_mks.append(mk_name)
            with open(mk_name, "w") as fmk:
                fmk.write(f"# Auto-generated by gen_android_bp.py for {pkg} symlinks\n\n")
                
                for mod in mods:
                    if mod["type"] == "symlink":
                        safe_name = f"alsa_ucm_conf_{mod['pkg']}_{sanitize_name(mod['rel_path'])}"
                        installed_location = f"usr/share/alsa/ucm2/{mod['rel_path']}"
                        target_path = f"$(TARGET_OUT_VENDOR)/{installed_location}"
                        parent_dir = Path(mod['rel_path']).parent
                        target_dir = f"$(TARGET_OUT_VENDOR)/usr/share/alsa/ucm2/{parent_dir}" if str(parent_dir) != "." else f"$(TARGET_OUT_VENDOR)/usr/share/alsa/ucm2"
                        
                        fmk.write("include $(CLEAR_VARS)\n")
                        fmk.write(f"LOCAL_MODULE := {safe_name}\n")
                        fmk.write("LOCAL_MODULE_CLASS := FAKE\n")
                        fmk.write("LOCAL_MODULE_TAGS := optional\n")
                        fmk.write("include $(BUILD_SYSTEM)/base_rules.mk\n")
                        fmk.write(f"$(LOCAL_BUILT_MODULE): $(LOCAL_PATH)/{mk_name}\n")
                        fmk.write(f"\t@echo \"Symlink: {safe_name}\"\n")
                        fmk.write(f"\tmkdir -p {target_dir}\n")
                        fmk.write(f"\tln -sf {mod['target']} {target_path}\n")
                        fmk.write(f"\ttouch $@\n\n")
        
        with open(bp_name, "w") as f:
            f.write(f"// Auto-generated by gen_android_bp.py for {pkg} configs\n\n")
            
            reqs = []
            for mod in mods:
                safe_name = f"alsa_ucm_conf_{mod['pkg']}_{sanitize_name(mod['rel_path'])}"
                reqs.append(safe_name)
                
                if mod["type"] == "file":
                    parent_dir = Path(mod["rel_path"]).parent
                    sub_dir = f"alsa/ucm2/{parent_dir}" if str(parent_dir) != "." else "alsa/ucm2"
                    
                    f.write(f"prebuilt_usr_share {{\n")
                    f.write(f"    name: \"{safe_name}\",\n")
                    f.write(f"    src: \"{mod['path']}\",\n")
                    filename = Path(mod['path']).name
                    f.write(f"    filename: \"{filename}\",\n")
                    f.write(f"    sub_dir: \"{sub_dir}\",\n")
                    f.write(f"    vendor: true,\n")
                    f.write(f"}}\n\n")
                elif mod["type"] == "symlink":
                    if not args.legacy:
                        installed_location = f"usr/share/alsa/ucm2/{mod['rel_path']}"
                        f.write(f"install_symlink {{\n")
                        f.write(f"    name: \"{safe_name}\",\n")
                        f.write(f"    installed_location: \"{installed_location}\",\n")
                        f.write(f"    symlink_target: \"{mod['target']}\",\n")
                        f.write(f"    vendor: true,\n")
                        f.write(f"}}\n\n")
                    
            f.write(f"phony {{\n")
            f.write(f"    name: \"alsa-ucm-conf-{pkg}\",\n")
            f.write(f"    required: [\n")
            for req in reqs:
                f.write(f"        \"{req}\",\n")
            f.write(f"    ],\n")
            f.write(f"}}\n\n")

    # 4. Generate the main Android.bp that includes the others
    with open("Android.bp", "w") as f:
        f.write("// Auto-generated by gen_android_bp.py\n")
        f.write("// Scans ucm2/ and groups configurations into specific packages.\n\n")
        
        if args.soong_namespace:
            f.write("soong_namespace {\n}\n\n")
            
        f.write("build = [\n")
        for bp in sorted(generated_bps):
            f.write(f"    \"{bp}\",\n")
        f.write("]\n")

    # 5. Generate the main Android.mk if legacy mode is enabled
    if args.legacy:
        # We always create Android.mk if --legacy is passed, even if no symlinks, just to be clean, or we skip if no generated_mks
        if generated_mks:
            with open("Android.mk", "w") as f:
                f.write("# Auto-generated by gen_android_bp.py\n\n")
                f.write("LOCAL_PATH := $(call my-dir)\n\n")
                for mk in sorted(generated_mks):
                    f.write(f"include $(LOCAL_PATH)/{mk}\n")
    else:
        # Clean up old MKs just in case
        for mk in Path(".").glob("Android*.mk"):
            mk.unlink()

    if skipped:
        with open("skipped-android.txt", "w") as f:
            f.write("# The following files/symlinks were skipped because their names contain spaces,\n")
            f.write("# which causes issues with the Android build system (Make/Soong).\n\n")
            for s in sorted(skipped):
                f.write(f"{s}\n")

    print(f"Main Android.bp has been generated to include {len(generated_bps)} sub-files.")
    if args.legacy and generated_mks:
        print(f"Legacy mode: Android.mk has been generated to include {len(generated_mks)} sub-makefiles for symlinks.")
    if skipped:
        print(f"Skipped {len(skipped)} files/symlinks with spaces (written to skipped-android.txt)")
    
    print("Packages created:")
    for pkg in sorted(pkg_map.keys()):
        mk_info = f" (+ Android_{pkg}.mk for symlinks)" if args.legacy and f"Android_{pkg}.mk" in generated_mks else ""
        print(f" - alsa-ucm-conf-{pkg} ({len(pkg_map[pkg])} items) -> Android_{pkg}.bp{mk_info}")

if __name__ == '__main__':
    main()
