# NG1Sigma-Textures

Tools for extracting, exporting, and viewing textures from **NINJA GAIDEN Σ** in the **NINJA GAIDEN: Master Collection** (Steam).

## Repository Structure

```text
NG1Sigma-Textures/
├── tools/
│   ├── ng_sigma_texture_tool_v2.py
│   └── ng_sigma_texture_gallery.py
├── README.md
└── LICENSE
```

## Requirements

- Windows 10 or Windows 11
- Python 3.12
- A Steam installation of NINJA GAIDEN: Master Collection

The Python tools use the standard library. No additional Python packages are required.

[Texconv](https://github.com/microsoft/DirectXTex/wiki/Texconv) can optionally be used for converting DDS textures to other image formats.

## Game File Structure

The relevant game files are located in the game's installation directory:

```text
NINJA GAIDEN Σ/
├── databin/
│   ├── bgm/
│   ├── movie/
│   ├── databin
│   └── load_img.bin
├── NINJA GAIDEN SIGMA.exe
└── steam_api64.dll
```

The main `databin` file contains archive entries referencing the game's data. Texture resources are stored in GT1G containers, which can contain multiple texture resources.

The tools support reading the archive, extracting GT1G containers, and exporting their textures as DDS files.

## Installation

Clone the repository:

```powershell
git clone https://github.com/NGNero/NG1Sigma-Textures.git
cd NG1Sigma-Textures
```

Or download the repository as a ZIP and extract it.

## Texture Extraction

### 1. Locate the game files

Find the installation directory of NINJA GAIDEN: Master Collection through Steam.

The main archive is located at:

```text
databin/databin
```

### 2. Inspect the databin archive

Use the texture tool to inspect the archive:

```powershell
py -3.12 .\tools\ng_sigma_texture_tool_v2.py --help
```

Use the available archive listing command to inspect its entries.

### 3. Extract GT1G containers

Use the tool's extraction functionality to extract the texture containers from the databin archive.

Keep the extracted containers in a separate directory, for example:

```text
textures/
├── ...
└── ...
```

### 4. Export DDS textures

The tool can export textures from GT1G containers into DDS files.

For batch exporting, the following PowerShell example processes every GT1G file in a directory:

```powershell
$tool = ".\tools\ng_sigma_texture_tool_v2.py"
$source = ".\textures"
$destination = ".\dds_original"

New-Item -ItemType Directory -Force -Path $destination | Out-Null

Get-ChildItem $source -Filter "*.GT1G" -File | ForEach-Object {
    $output = Join-Path $destination $_.BaseName

    New-Item -ItemType Directory -Force -Path $output | Out-Null

    Write-Host "Exporting $($_.Name)..."

    & py -3.12 $tool export-dds $_.FullName --out $output

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Failed to export $($_.Name)"
    }
}
```

The resulting DDS files can be opened with compatible image editors or viewed using the gallery tool.

## Texture Gallery

The gallery tool generates a local HTML gallery for browsing exported DDS textures.

Generate a gallery:

```powershell
py -3.12 .\tools\ng_sigma_texture_gallery.py ".\dds_original" --out ".\texture_gallery"
```

Open the gallery:

```powershell
start .\texture_gallery\index.html
```

To generate a smaller gallery for testing:

```powershell
py -3.12 .\tools\ng_sigma_texture_gallery.py ".\dds_original" --out ".\texture_gallery_test" --limit 150
```

The gallery generates thumbnails and displays supported texture previews.

### Supported formats

- DXT1
- DXT5
- BGRA8
- RGBA8

RAW8 textures can be detected, but palette-based previews are not currently supported.

## Texture Formats and Types

Not every texture is intended to look like a regular image.

Common texture types include:

| Type | Purpose |
|---|---|
| Diffuse / Albedo | Main visible colors and surface appearance |
| Normal maps | Surface direction and lighting information |
| Texture atlases | Multiple texture regions packed into one image |
| Material maps | Surface properties such as specular or roughness data |
| Masks | Control specific material or rendering effects |

Normal maps and material maps should not be treated like ordinary photographs. Modifying them without understanding their purpose can produce incorrect lighting or visual artifacts.

## Editing and Upscaling Textures

Textures can be exported to formats such as PNG using Texconv.

Example:

```powershell
texconv -ft png -o .\png_original .\dds_original\texture.dds
```

After editing a texture, convert it back to DDS.

For DXT1:

```powershell
texconv -f DXT1 -m 0 -o .\dds_modified .\png_modified\texture.png
```

For DXT5:

```powershell
texconv -f DXT5 -m 0 -o .\dds_modified .\png_modified\texture.png
```

**Important:**
- Preserve the original texture dimensions unless the game supports different dimensions.
- Preserve the original compression format where possible.
- Check whether the original texture uses an alpha channel.
- Preserve mipmaps when required.
- Do not apply ordinary image upscaling to normal maps or packed material textures without understanding their channel data.

The correct DDS format depends on the original texture. DXT1 and DXT5 are examples, not universal settings.

## Rebuilding GT1G Containers

The texture tool also includes functionality for rebuilding GT1G containers from exported resources.

This functionality is experimental. Rebuilding containers does not guarantee that modified textures will work correctly in the game.

The archive patching functionality is also experimental and has not been verified as a complete in-game replacement workflow.

## Limitations

- RAW8 palette-based texture previews are not supported.
- Not all texture types can be identified automatically.
- Texture editing and AI upscaling are not included.
- Rebuilding and patching archives have not been validated in-game.

## Credits

- **[Mystixor/NGMCToolbox](https://github.com/Mystixor/NGMCToolbox)** - Reference implementation used to understand the game's archive and texture formats.

## Disclaimer

This project is an independent research and modding tool. It is not affiliated with or endorsed by Koei Tecmo or Team Ninja.

The tools are intended for use with game files obtained by the user. Do not redistribute copyrighted game assets or extracted textures.

Everything is experimental!!
