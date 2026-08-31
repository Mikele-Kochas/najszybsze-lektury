# Dodatkowe certyfikaty CA (opcjonalne)

Katalog jest **domyślnie pusty** i na zwykłej maszynie nie zmienia nic w budowaniu obrazu.

Przydaje się, gdy w sieci działa przechwytywanie TLS — antywirus ze skanowaniem HTTPS
(Avast, ESET, Kaspersky, Bitdefender) albo firmowe proxy (Zscaler, Fortinet). Host ufa wtedy
podstawianemu certyfikatowi, ale kontener już nie, więc `pip install` przerywa build błędem:

```
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: unable to get local issuer certificate'))
```

## Jak to naprawić

Wyeksportuj certyfikat główny do tego katalogu jako plik **`.crt` w formacie PEM**,
po czym zbuduj obraz ponownie. Dockerfile sam zainstaluje wszystko, co tu leży.

Windows, PowerShell (podmień odcisk palca na własny):

```powershell
$c = Get-ChildItem Cert:\LocalMachine\Root |
     Where-Object { $_.Subject -like "*Avast*" } | Select-Object -First 1
$pem = "-----BEGIN CERTIFICATE-----`n" +
       [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks') +
       "`n-----END CERTIFICATE-----`n"
Set-Content -Path certs\proxy-root.crt -Value $pem -Encoding ascii
```

Alternatywa bez zmiany obrazu: wyłącz na czas budowania skanowanie HTTPS w antywirusie.

> Certyfikaty z tego katalogu trafiają do obrazu. Nie commituj ich do repozytorium
> ani nie publikuj obrazu zbudowanego z certyfikatem firmowego proxy.
