# Mod Listesi Çeviri Paketi Hazırlama

Bu rehber, bu aracı kendi Skyrim mod listesi çeviri paketi için kullanmak isteyen
kişiler için kısa bir başlangıç notudur.

Araç son kullanıcı tarafında Nexus araması yapmaz. Hazır bir manifest dosyasını
okur, bu manifestte belirtilen NexusMods dosyalarını indirir, indirilen çeviri
arşivlerini işler ve seçilen MO2 mod listesinin `mods` klasörüne hazır bir çeviri
modu oluşturur.

## Temel Mantık

- Her release tek bir mod listesi veya tek bir manifest için hazırlanır.
- Manifest içinde hangi modun hangi çeviri dosyasıyla eşleştiği açıkça yazılır.
- Son kullanıcı manifest seçmez; release içine gömülü manifest otomatik yüklenir.
- API anahtarı, kişisel dosya yolu veya geçici indirme bilgisi manifestte tutulmaz.
- Araç MO2 profilini ve `modlist.txt` dosyasını otomatik değiştirmez.

## Release Klasörü

Önerilen yapı:

```text
src/modlist_translation_wizard/resources/releases/<release-id>/
  manifest.json
  manifest.json.sha256
  branding.json
  banner.png
  icon.ico
```

İsteğe bağlı olarak release içine yerel kaynaklar eklenebilir:

```text
src/modlist_translation_wizard/resources/releases/<release-id>/
  curated_sources/
    dsd_output/
    dsd_database/
```

## Minimal Manifest Örneği

Aşağıdaki örnek gerçek bir tam manifest değildir, sadece alanların nasıl
yerleştiğini göstermek için kısaltılmıştır.

```json
{
  "schema_version": "mtt-wizard-manifest.v2",
  "manifest_id": "example-list-tr-stable",
  "release_state": "DRAFT",
  "modlist": {
    "id": "example-list",
    "name": "Example List",
    "version": "1.0.0",
    "supported_profiles": ["Default"],
    "profile_fingerprint_sha256": ""
  },
  "language": "tr",
  "channel": "stable",
  "output": {
    "mod_name": "Example List - Turkce Ceviri",
    "install_mode": "STAGED_MO2_MOD",
    "profile_activation_requires_confirmation": true
  },
  "nexus": {
    "discovery_enabled": false,
    "request_scope": "KNOWN_MOD_AND_FILE_IDS_ONLY",
    "authentication": {
      "manual_api_key": "USER_PROVIDED",
      "secret_storage": "OS_CREDENTIAL_STORE"
    },
    "delivery": {
      "premium_api": "SUPPORTED",
      "non_premium_nxm": "SUPPORTED"
    }
  },
  "summary": {
    "target_count": 1,
    "entry_count": 1,
    "artifact_reference_count": 1,
    "unique_download_count": 1
  },
  "entries": [
    {
      "target_id": "target-example-plugin",
      "target": {
        "path": "ExamplePlugin.esp",
        "normalized_path": "exampleplugin.esp",
        "type": "PLUGIN"
      },
      "base": {
        "name": "Example Mod",
        "version": "1.0",
        "nexus_mod_id": 1000,
        "nexus_file_id": 2000
      },
      "selection": {
        "status": "APPROVED",
        "confidence": "VERIFIED_CURATED",
        "translation_name": "Example Mod Turkish Translation"
      },
      "install": {
        "mode": "DSD_CONVERT"
      },
      "artifacts": [
        {
          "artifact_id": "nexusmods:skyrimspecialedition:3000:4000",
          "source": "CURATED",
          "game_domain": "skyrimspecialedition",
          "translation_nexus_mod_id": 3000,
          "translation_file_id": 4000,
          "translation_file_name": "Example Mod Turkish Translation.zip",
          "install_mode": "DSD_CONVERT",
          "source_url": "https://www.nexusmods.com/skyrimspecialedition/mods/3000?tab=files&file_id=4000"
        }
      ]
    }
  ],
  "add_on_packages": [
    {
      "id": "example-extra-pack",
      "name": "Example List Turkce Ek Paketi",
      "enabled": true,
      "required": false,
      "game_domain": "skyrimspecialedition",
      "translation_nexus_mod_id": 5000,
      "translation_file_id": 6000,
      "translation_file_name": "Example List - Turkce Ek Paketi.zip",
      "install_mode": "OUTPUT_MOD_OVERLAY",
      "apply_order": 100000,
      "source_url": "https://www.nexusmods.com/skyrimspecialedition/mods/5000?tab=files&file_id=6000"
    }
  ]
}
```

## Başlangıç Akışı

1. Mod listesinin MO2 profilini belirleyin.
2. Çevrilecek plugin, interface, strings ve script hedeflerini çıkarın.
3. NexusMods üzerinde kullanılacak Türkçe çeviri dosyalarının `mod_id` ve
   `file_id` bilgilerini doğrulayın.
4. Manifestte her hedef için `entries` ve `artifacts` alanlarını oluşturun.
5. Varsa tamamlayıcı paketi `add_on_packages` içine ekleyin.
6. `branding.json`, banner ve icon dosyalarını release klasörüne koyun.
7. Manifesti test edin, indirme ve hazırlama akışını temiz bir MO2 profilinde
   doğrulayın.
8. Standalone release üretin.

## Build Örneği

PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_standalone.ps1 `
  -ReleaseSource "src\modlist_translation_wizard\resources\releases\<release-id>" `
  -WindowsResources `
  -AssumeYesForDownloads
```

Çıktı:

```text
dist\standalone\CeviriAraci.exe
dist\standalone.zip
```

## Dikkat Edilecekler

- Manifest içine kişisel bilgisayar yolları yazmayın.
- API key, session token veya geçici Nexus indirme linki paylaşmayın.
- Son kullanıcı aracında Nexus discovery veya scraping yapmayın.
- Mümkünse premium ve ücretsiz/tarayıcı indirme akışını ayrı ayrı test edin.
- Çıktı modunu MO2 içinde kullanıcı aktif etmelidir; profil otomasyonu dikkatli
  ve kullanıcı onaylı yapılmalıdır.
- Script dosyaları sürüm hassas olabilir. Sadece uyumlu olduğundan emin olunan
  script çevirilerini dahil edin.

