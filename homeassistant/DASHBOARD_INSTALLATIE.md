# HA Dashboard installeren

1. Ga in Home Assistant naar **Instellingen → Dashboards → + Dashboard toevoegen**
2. Kies **Leeg dashboard**, geef het de naam **Bitvavo**
3. Open het dashboard → klik op de **potlood** (bewerken) rechtsboven
4. Klik op de **drie puntjes** → **Raw configuratie-editor**
5. Verwijder de bestaande inhoud en plak de inhoud van `dashboard.yaml`
6. Klik **Opslaan**

De sensoren verschijnen zodra de bot één cyclus heeft gedraaid en data via MQTT heeft gestuurd.
