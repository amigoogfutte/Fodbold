# Kampdag – fodbold

App til at holde styr på stævner, kampe, mål og kort. Den kører i Home Assistant, og data deles mellem alle brugere i Home Assistant.

- **Stævner** kan være udendørs, indendørs eller over flere dage. Kampe kan lægges ind hjemmefra eller tilføjes undervejs.
- **Klubber** har navn og logo. Logoet kan vælges som et billede eller hentes fra et link (direkte til billedet eller til klubbens hjemmeside).
- **Live:** Tryk på "+ MÅL" og "Gult" eller "Rødt" kort for hvert hold. Kampminuttet gemmes og kan rettes bagefter.
- **Samme mål på to telefoner** inden for 10 sekunder tæller som ét mål. Flere mål fra samme telefon tæller altid hver for sig.
- **Én styrer pr. stævne:** Den, der starter kampen, styrer stævnet live. De andre kan kun kigge med, indtil kampen er slut, eller indtil de trykker "Overtag". Samme hold kan kun have én kamp i gang ad gangen.
- **Turneringens spiller** (pokal) kan skrives på hvert stævne.
- **Kun for ejeren** af Home Assistant ⚙️: lav et delelink, så andre kan følge med uden at logge ind (kun læse). Ejeren kan også lave et billede af dagens kampe eller hele turneringen, klar til Facebook.
- Appen virker uden dækning. Ændringer gemmes på telefonen og sendes, når der er forbindelse igen.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/fodbold` som type **Integration**.
2. Installer **Kampdag – fodbold** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Kampdag*.

Appen findes i sidemenuen under **Fodbold** og på `https://<din-home-assistant>/fodbold-app/index.html` (kort adresse: `/kampdag`).
På iPhone: Åbn adressen i Safari → Del → **Føj til hjemmeskærm**.

Delelinket virker kun uden for hjemmet, hvis Home Assistant kan nås udefra (fx via Home Assistant Cloud).

## Automation ved mål

For hvert nyt mål sender integrationen hændelsen `fodbold_maal` med felterne `hvem` (`os`/`dem`), `hold`, `vores_klub`, `modstander`, `stilling` (fx `2-1`), `minut`, `staevne` og `registreret_af`.
Den kan for eksempel bruges til at sende en besked til telefonen:

```yaml
triggers:
  - trigger: event
    event_type: fodbold_maal
actions:
  - action: notify.mobile_app_din_telefon
    data:
      title: "⚽ Mål til {{ trigger.event.data.hold }}"
      message: "{{ trigger.event.data.vores_klub }} – {{ trigger.event.data.modstander }} {{ trigger.event.data.stilling }}"
```
