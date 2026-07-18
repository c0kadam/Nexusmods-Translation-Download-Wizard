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

## Branding

`branding.json`, release adını ve banner görünümünü belirler:

```json
{
  "display_name": "Example List Türkçe Çeviri Aracı",
  "subtitle": "Example List için hazırlanmış çeviri paketi",
  "banner": "banner.png",
  "icon": "icon.ico",
  "accent_color": "#2F5F73",
  "font_color": "#C8D0D3",
  "font_shadow": "#0B1116",
  "warm_glow": "#8A3F1B"
}
```

Renkler yalnızca `#RRGGBB` biçiminde kabul edilir. Geçersiz veya eksik değerler
güvenli varsayılan renklere döner. `accent_color` banner zeminini,
`font_color` banner metnini, `font_shadow` başlık gölgesini ve seçili görünüm
düğmesini, `warm_glow` ise banner çerçevesiyle vurgu durumlarını belirler.

## Uzaktan Manifest Kanalı

Manifesti her uygulama build'inde yeniden paketlemek istemiyorsanız release klasörüne
`remote_manifest.json` ekleyebilirsiniz. Bu dosya aracın açılışta GitHub gibi güvenilir
bir kaynaktan güncel manifest listesini kontrol etmesini sağlar.

Örnek `remote_manifest.json`:

```json
{
  "schema_version": "mtw-remote-manifest-config.v1",
  "enabled": true,
  "list_id": "example-list",
  "remote_list_id": "ExampleList",
  "channel": "stable",
  "repository": "c0kadam/NTDW-TranslationMAPS",
  "branch": "main",
  "index_path": "{remote_list_id}/{channel}/index.json",
  "allow_hosts": ["raw.githubusercontent.com"],
  "cache_ttl_seconds": 0,
  "timeout_seconds": 8,
  "allow_stale_cache": false
}
```

Bu ayar varsa GUI iki kaynak modu sunar:

- `OTA (Güncel)`: Her uygulama oturumunda uzak kanaldan yeni indeks ve manifest
  indirir. Güncel veri alınamazsa önbelleğe veya yerel manifeste sessiz geçiş yapmaz.
- `Yerel`: Hiçbir manifest ağ isteği yapmadan release içindeki manifesti veya
  uygulamaya gömülü manifesti kullanır. Bu moda geçildiğinde OTA önbelleği temizlenir.

İndirilen OTA manifesti yalnızca açık uygulama oturumunda kullanılır. Uygulama normal
şekilde kapatıldığında önbellek silinir; sonraki açılışta manifest yeniden indirilir.

Kaynak değiştirildiğinde eski profil kontrolü ve indirme durumu sıfırlanır. Seçili
profil yeni manifest ile yeniden doğrulanır. İndirme planına yazılan çalışma manifesti,
GUI'de gösterilen manifestin birebir doğrulanmış kopyasıdır.

Standalone çıktıda `release/` kullanıcıya açık tek release klasörüdür. Tam manifest,
hash, branding ve OTA ayarı burada bulunur. `modlist_translation_wizard/` klasörü
Nuitka çalışma zamanı dosyalarıdır; kullanıcı tarafından değiştirilmemelidir. Dahili
tarafta yalnızca release kimliği ve OTA bağlantısı için küçük bootstrap ayarları tutulur,
ikinci bir tam manifest kopyası oluşturulmaz.

Önerilen remote repo yapısı:

```text
Lorerim/
  index.json
  stable/
    manifest.json
    manifest.json.sha256
    changelog.md
NordicSouls/
  index.json
  stable/
    manifest.json
    manifest.json.sha256
    changelog.md
```

Bu yapıda her mod listesinin kendi `index.json` dosyası vardır. LoreRim için araç
şu dosyayı okur:

```text
https://raw.githubusercontent.com/c0kadam/NTDW-TranslationMAPS/main/Lorerim/index.json
```

`Lorerim/index.json` örneği:

```json
{
  "schema_version": "mtw-remote-manifest-index.v1",
  "list_id": "lorerim",
  "manifest": {
      "channel": "stable",
      "version": "2026-07-15",
      "url": "https://raw.githubusercontent.com/c0kadam/NTDW-TranslationMAPS/main/Lorerim/stable/manifest.json",
      "sha256": "<manifest.json sha256>",
      "min_app_version": "0.1.0"
  }
}
```

Bir mod listesi için birden fazla kanal gerekiyorsa `manifest` yerine `manifests`
listesi de kullanılabilir.

Güvenlik kuralları:

- Remote URL'ler HTTPS olmalıdır.
- Manifest URL host'u `allow_hosts` içinde olmalıdır.
- İndirilen manifest SHA-256 ile doğrulanır.
- Manifest şeması normal yerel manifest gibi doğrulanır.
- Remote içerik kod olarak çalıştırılmaz; sadece JSON veri olarak okunur.
- Nexus dosyaları yine manifestteki `file_id`, boyut ve hash bilgilerine göre işlenir.

Ek paketi güncellediğinizde önerilen akış:

1. NexusMods'a yeni dosyayı yükleyin.
2. Yeni `file_id`, dosya boyutu ve SHA-256 değerini manifestte güncelleyin.
3. Manifestin `version` bilgisini artırın.
4. `manifest.json.sha256` ve remote `index.json` içindeki SHA-256 değerini yenileyin.
5. Remote repo'yu güncelleyin.

Bu akışta kullanıcı yeni exe indirmek zorunda kalmadan güncel manifesti alabilir.

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

### 1. Üst Bilgi

Bu bölüm manifestin kimliğini, hedef dili ve release durumunu belirtir.
`release_state` hazırlık aşamasında `DRAFT`, yayımlanacak paketlerde
`STABLE` gibi tutulabilir.

```json
{
  "schema_version": "mtt-wizard-manifest.v2",
  "manifest_id": "example-list-tr-stable",
  "release_state": "DRAFT",
  "language": "tr",
  "channel": "stable"
}
```

### 2. Mod Listesi Bilgisi

Bu bölüm aracın hangi mod listesi ve hangi profil için hazırlandığını gösterir.
`profile_fingerprint_sha256` boş bırakılabilir, ancak dolu olduğunda araç profil
uyumluluğunu daha net kontrol edebilir.

```json
{
  "modlist": {
    "id": "example-list",
    "name": "Example List",
    "version": "1.0.0",
    "supported_profiles": ["Default"],
    "profile_fingerprint_sha256": ""
  }
}
```

### 3. Çıktı Modu

Bu bölüm çeviri paketinin MO2 `mods` klasörü altında hangi isimle
oluşturulacağını belirtir. Araç profili otomatik aktif etmez; kullanıcıdan onay
veya manuel işlem beklenir.

```json
{
  "output": {
    "mod_name": "Example List - Turkce Ceviri",
    "install_mode": "STAGED_MO2_MOD",
    "profile_activation_requires_confirmation": true
  }
}
```

### 4. Nexus İndirme Davranışı

Son kullanıcı aracı Nexus üzerinde arama yapmaz. Buradaki ayarlar aracın sadece
manifestte verilmiş `mod_id` ve `file_id` değerleriyle çalışacağını belirtir.
API anahtarı manifest içine yazılmaz; kullanıcı arayüzünden alınır.

```json
{
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
  }
}
```

### 5. Özet Sayılar

Bu alanlar kullanıcıya ve testlere genel kapsam bilgisi verir. Büyük manifestlerde
gerçek değerlerle güncel tutulması önerilir.

```json
{
  "summary": {
    "target_count": 1,
    "entry_count": 1,
    "artifact_reference_count": 1,
    "unique_download_count": 1
  }
}
```

### 6. Tek Bir Çeviri Hedefi

`entries` içindeki her öğe bir hedefi temsil eder. Hedef bir plugin, interface
dosyası, strings dosyası veya başka bir native dosya olabilir.

`target` çevrilecek dosyayı, `base` mod listesindeki orijinal modu,
`selection` seçilen çeviri kararını, `install` ise nasıl işleneceğini belirtir.

```json
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
  }
}
```

### 7. İndirilecek Çeviri Dosyası

`artifacts` bölümü hedef için indirilecek NexusMods dosyasını tanımlar.
Buradaki `translation_nexus_mod_id` ve `translation_file_id` değerleri doğruysa
araç canlı arama yapmadan dosyayı indirebilir.

```json
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
```

### 8. Ek Paket

`add_on_packages` opsiyoneldir. Mod listesi için hazırlanmış tamamlayıcı bir
paket varsa burada tanımlanabilir. `OUTPUT_MOD_OVERLAY`, paketin çeviri çıktısının
üzerine uygulanacağını belirtir.

```json
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
```

### Toplu Minimal Örnek

Yukarıdaki parçalar birleştirildiğinde minimal manifest iskeleti şu yapıya
yaklaşır:

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
