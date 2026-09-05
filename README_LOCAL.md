# MapMeet – uruchomienie lokalne (Windows 10)

Ten dokument opisuje jak uruchomić projekt **MapMeet** w pełni lokalnie, bez zależności od infrastruktury Emergent.

## Wymagania wstępne

Zainstaluj następujące narzędzia (kolejność dowolna):

| Narzędzie | Wersja | Link |
|---|---|---|
| **Python** | 3.11 lub 3.12 (64-bit) | https://www.python.org/downloads/windows/ |
| **Node.js** | **20 LTS** (nie 22, nie 18) | https://nodejs.org/en/download |
| **MongoDB Community Server** | 6.x lub 7.x | https://www.mongodb.com/try/download/community |
| **Git** (opcjonalnie) | dowolna | https://git-scm.com/download/win |

Podczas instalacji:
- **Python**: zaznacz „Add python.exe to PATH".
- **Node.js**: pozostaw domyślne opcje, w tym „Add to PATH" i „npm".
- **MongoDB**: wybierz **Complete** oraz zaznacz „Install MongoDB as a Service" – dzięki temu serwer wystartuje automatycznie na `localhost:27017`.

Sprawdź instalacje w PowerShell:

```powershell
python --version         # 3.11.x lub 3.12.x
node --version           # v20.x.x
npm --version            # 10.x
mongod --version         # MongoDB 6.x/7.x
```

## Struktura projektu

```
mapmeet/
├── backend/            # FastAPI + MongoDB
│   ├── server.py
│   ├── poland_geo.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/           # React 19 + Leaflet
│   ├── src/
│   ├── package.json
│   └── .env.example
└── README_LOCAL.md     # ten plik
```

## 1) Uruchomienie MongoDB

Jeśli instalator MongoDB założył usługę, powinna już działać. Weryfikacja:

```powershell
Get-Service MongoDB
```

Powinno zwrócić `Status: Running`. Jeśli nie:

```powershell
Start-Service MongoDB
```

Alternatywnie, ręczny start:

```powershell
mongod --dbpath "C:\data\db"
```

(Utwórz katalog `C:\data\db` jeśli nie istnieje.)

Bazę danych **`mapmeet_db`** utworzy sam backend przy pierwszym uruchomieniu, wraz z indeksami i danymi seed.

## 2) Backend – FastAPI

Otwórz PowerShell w katalogu projektu:

```powershell
cd backend

# 1. Utwórz wirtualne środowisko
python -m venv .venv

# 2. Aktywuj je
.\.venv\Scripts\Activate.ps1

# UWAGA: jeśli PowerShell zablokuje skrypt, uruchom jednorazowo:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 3. Zainstaluj zależności
pip install --upgrade pip
pip install -r requirements.txt

# 4. Skopiuj przykładowy plik .env
copy .env.example .env

# 5. Uruchom serwer
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

Backend powinien wystartować na `http://localhost:8000`. Sprawdź:

```powershell
curl http://localhost:8000/api/
# {"app":"MapMeet","status":"ok"}
```

Domyślne konta seedowane przy pierwszym uruchomieniu:

| Rola | Email | Hasło |
|---|---|---|
| Administrator | `admin@mapmeet.pl` | `Admin123!` |
| Użytkownik demo | `demo@mapmeet.pl` | `Demo123!` |

## 3) Frontend – React

W nowym oknie PowerShell:

```powershell
cd frontend

# 1. Skopiuj przykładowy plik .env
copy .env.example .env

# 2. Zainstaluj zależności (użyj `npm ci` gdy chcesz zainstalować dokładnie
#    wersje z package-lock.json – zalecane w CI/CD).
npm install

# 3. Uruchom aplikację w trybie deweloperskim
npm start

# 4. (opcjonalnie) Zbuduj wersję produkcyjną
npm run build
```

Aplikacja otworzy się w przeglądarce pod `http://localhost:3000` i połączy się z backendem na `http://localhost:8000`.

> **Uwaga:** projekt jest kompatybilny z **Node.js 20 LTS**. `npm install` przechodzi bez `--legacy-peer-deps` ani `--force`. Ostrzeżenie `[visual-edits] @emergentbase/visual-edits not installed` jest nieszkodliwe – ten plugin działa tylko w środowisku Emergent i jest opcjonalny.

## 4) Zmienne środowiskowe

### `backend/.env`

| Zmienna | Domyślnie (lokalnie) | Opis |
|---|---|---|
| `MONGO_URL` | `mongodb://localhost:27017` | Adres MongoDB. |
| `DB_NAME` | `mapmeet_db` | Nazwa bazy. |
| `CORS_ORIGINS` | `http://localhost:3000` | Dozwolone adresy frontendu (rozdzielone przecinkiem). |
| `JWT_SECRET` | *(losowy ciąg)* | Sekret do podpisywania tokenów JWT. **Zmień w produkcji.** |
| `COOKIE_SECURE` | `false` | Wymaga HTTPS gdy `true`. Lokalnie `false`. |
| `COOKIE_SAMESITE` | `lax` | Polityka SameSite dla ciasteczka `access_token`. |
| `ADMIN_EMAIL` | `admin@mapmeet.pl` | Email seedowanego admina. |
| `ADMIN_PASSWORD` | `Admin123!` | Hasło seedowanego admina. |

### `frontend/.env`

| Zmienna | Domyślnie (lokalnie) | Opis |
|---|---|---|
| `REACT_APP_BACKEND_URL` | `http://localhost:8000` | Adres backendu widoczny z przeglądarki. |
| `WDS_SOCKET_PORT` | `3000` | Port webpack-dev-server sockets. |
| `ENABLE_HEALTH_CHECK` | `false` | Wyłącz specyficzne pluginy Emergent. |

## 5) Skrypty pomocnicze

### Reset bazy danych

Aby wyczyścić dane i wygenerować od nowa seed 12 wydarzeń:

```powershell
mongosh
> use mapmeet_db
> db.dropDatabase()
> exit
```

Następnie zrestartuj backend – seed uruchomi się ponownie.

### Utworzenie produkcyjnego buildu frontendu

```powershell
cd frontend
npm run build
```

Wynik pojawi się w `frontend/build/` – można go serwować dowolnym statycznym serwerem HTTP (nginx, IIS, `serve` z npm itp.).

## 6) Typowe problemy

**„MongoDB connection refused" w logach backendu**  
Upewnij się, że usługa MongoDB działa: `Get-Service MongoDB`. Zrestartuj jeśli trzeba: `Restart-Service MongoDB`.

**„Cannot find module '@emergentbase/visual-edits/craco'" podczas `npm start`**  
Uśmiech spokoju – ten warning jest nieszkodliwy. Plik `craco.config.js` łapie go w try/catch i wyłącza plugin (potrzebny tylko w środowisku Emergent).

**Cookies nie zapisują się / „401 Brak autoryzacji" po logowaniu**  
W `backend/.env` upewnij się, że masz `COOKIE_SECURE=false` i `COOKIE_SAMESITE=lax`. Ustawienia `secure=true`/`samesite=none` wymagają HTTPS i nie zadziałają na `http://localhost`.

**„CORS preflight not allowed"**  
Sprawdź, że `CORS_ORIGINS` w `backend/.env` zawiera dokładnie `http://localhost:3000` (bez ukośnika końcowego).

**Port 8000 lub 3000 zajęty**  
Zmień port uruchomienia:
- Backend: `uvicorn server:app --reload --host 0.0.0.0 --port 8010`  
  Wtedy w `frontend/.env`: `REACT_APP_BACKEND_URL=http://localhost:8010`
- Frontend: `set PORT=3005 && npm start` (cmd) lub `$env:PORT=3005; npm start` (PowerShell)  
  Wtedy w `backend/.env`: `CORS_ORIGINS=http://localhost:3005`

**Import Error: No module named 'poland_geo'**  
Uruchom `uvicorn` z katalogu `backend`, nie z korzenia repo.

**Rate limit trafiony przy testach („429 Zbyt wiele żądań")**  
Poczekaj minutę – domyślne limity to 10 req/min dla rejestracji, 20/min dla logowania.

## 7) Endpointy API (krótkie streszczenie)

Wszystkie endpointy są prefiksowane `/api`.

| Metoda | Ścieżka | Auth | Opis |
|---|---|---|---|
| POST | `/api/auth/register` | – | Rejestracja użytkownika |
| POST | `/api/auth/login` | – | Logowanie (ustawia cookie) |
| POST | `/api/auth/logout` | ✓ | Wylogowanie |
| GET | `/api/auth/me` | ✓ | Bieżący użytkownik |
| PATCH | `/api/auth/profile` | ✓ | Aktualizacja profilu |
| GET | `/api/events` | opt | Lista wydarzeń (filtry: `category`, `from_date`, `to_date`, `only_public`, `include_archived`, `near_lat`, `near_lon`, `max_km`, `search`) |
| POST | `/api/events` | ✓ | Utworzenie wydarzenia (walidacja granic Polski) |
| GET | `/api/events/{id}` | opt | Szczegóły |
| DELETE | `/api/events/{id}` | ✓ | Usunięcie (organizator lub admin) |
| POST | `/api/events/{id}/join` | ✓ | Dołączenie |
| POST | `/api/events/{id}/leave` | ✓ | Opuszczenie |
| POST | `/api/events/{id}/approve/{user_id}` | ✓ | Akceptacja (organizator) |
| POST | `/api/events/{id}/reject/{user_id}` | ✓ | Odrzucenie (organizator) |
| GET/POST | `/api/events/{id}/comments` | opt/✓ | Komentarze |
| POST | `/api/events/{id}/invite` | ✓ | Generowanie linku zapraszającego |
| GET | `/api/events/by_invite/{token}` | – | Podgląd wydarzenia z tokenu |
| POST | `/api/events/by_invite/{token}/join` | ✓ | Dołączenie przez token |
| GET | `/api/notifications` | ✓ | Powiadomienia |
| POST | `/api/notifications/{id}/read` | ✓ | Oznacz przeczytane |
| POST | `/api/notifications/read-all` | ✓ | Oznacz wszystkie |
| GET | `/api/calendar` | opt | Wydarzenia pogrupowane po dniach |
| POST | `/api/reports` | ✓ | Zgłoszenie wydarzenia/komentarza |
| GET | `/api/admin/stats` | admin | Statystyki |
| GET | `/api/admin/users` | admin | Lista użytkowników |
| POST | `/api/admin/users/{id}/block` | admin | Toggle blokady |
| GET | `/api/admin/reports` | admin | Lista zgłoszeń |
| POST | `/api/admin/reports/{id}/resolve` | admin | Zamknij zgłoszenie |
| DELETE | `/api/admin/comments/{id}` | admin | Usuń komentarz |
| GET | `/api/poland/check?lat=&lon=` | – | Sprawdza czy punkt leży w Polsce |

Dokumentacja Swagger dostępna po starcie backendu pod `http://localhost:8000/docs`.

---

Powodzenia! Miłej pracy z MapMeet 🇵🇱
