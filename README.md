# Coastal Storm Response Kit

The storm response tools Alaska DOT&PF built for the Typhoon Halong response,
packaged so another organization can stand up its own copy in ArcGIS Online
for its own event. This folder holds the kit and the deployer, and nothing
else.

You sign in, name your event, tick the tools you want and press deploy. A few
minutes later you have a folder of new, empty, working items in your own
organization, and a Hub site that links them together.

## What you get

| Tool | What is created |
| --- | --- |
| Hub site | A landing page with a section and an embedded app for each tool you deployed |
| Team Status Dashboard | The team update survey, its layer and views, a map, the dashboard and a read-only twin |
| Community Status Dashboard | The impacted communities layer and views, a map and dashboard, and an editor app with its two surveys |
| FEMA Damage Assessment Dashboard | The home damage assessment survey, its layer and views, the repair inspection layer, a map and the dashboard |
| Drone Video | Video Lines and Video Services layers with their public views, a map for the player, and a notebook that fills the lines from a Video Server |
| Drone Imagery Comparison | A map with a Before and an After imagery layer, and a swipe app over it |
| Area-based requests | A layer, map, request app and tracker dashboard for asking for drone footage, drone imagery, or a home or infrastructure assessment over an area you draw |

All seven together make 43 items. Every layer is created empty: no records
from the original event come along.

## What you need

- Windows with **ArcGIS Pro** installed, signed in to the ArcGIS Online
  organization you want the tools in. The deployer borrows that sign-in, so it
  never asks for a password.
- An account there that can create content and publish hosted feature layers
  (the Publisher role or higher).
- Nothing to install. The deployer runs on the Python that ships with ArcGIS
  Pro.

## Deploy

1. Double-click `deploy.cmd`. A page opens at `http://127.0.0.1:47620`. It
   answers this computer only.
2. Choose the organization to deploy into. The page lists the organizations
   ArcGIS Pro is signed in to.
3. Name the event. The name goes in front of every title and names the folder.
4. Tick the tools. Drone Imagery Comparison and Drone Video have optional
   boxes for your imagery service, your Video Server and your hosted player.
5. Choose who can see it: only you, or your organization.
6. Press **Review what will be created**, read the list, then deploy. The log
   streams as it runs and ends with links to everything it made.

Close the Command Prompt window to stop the page.

### From the command line

`propy` is ArcGIS Pro's Python, found at
`C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat`.

```
propy deployer\deploy.py --portal https://www.arcgis.com --whoami
propy deployer\deploy.py --portal https://www.arcgis.com --event "Spring Flood 2027" --tools team_status,fema_damage --plan
propy deployer\deploy.py --portal https://www.arcgis.com --event "Spring Flood 2027" --tools team_status,fema_damage
```

Tool keys: `hub_site`, `imagery_compare`, `drone_video`, `team_status`,
`fema_damage`, `community_status`, `request_intake`. `--plan` lists what
would be created and stops. Add `--share org` to share with your
organization; the default is private. The optional addresses go in as
`--option name=value`: `imagery_after_url`, `video_server_url`,
`video_player_url`.

## What a deployment does

- Creates one folder, `<event> Storm Response Kit`, and puts everything in it.
- Creates the hosted layers from their definitions, then the views over them,
  then the maps, dashboards, surveys and apps, pointing each at the new copies.
- Builds the Hub site from the tools you picked, with a private group that
  holds everything the run created.
- Reads everything back and reports problems: a layer that does not answer,
  anything left unfilled, a dashboard reading a layer its map does not have.
- Writes a run file to `deployer\runs\` listing what it made and where.

It changes nothing that already exists in your organization.

## After you deploy

A few things no script can supply:

- **Your drone imagery.** Without an imagery service, the comparison app uses
  a stand-in "After" layer. Swap it for your own in the Imagery Comparison Map.
- **The video player.** `video-player\VideoPlayer.html` is a web page that
  needs a web host and an ArcGIS OAuth app. Copy `config.example.json` to
  `config.json` beside it and fill it in; the run file holds the values for
  your deployment under `video_player_config`. The player does not handle 360
  footage yet.
- **The video lines.** Open the Update Video Lines notebook, check the Video
  Server address in its first cell, and run it or schedule it.
- **Your logo.** The Community Status Dashboard shows a blank placeholder
  image titled "Organization logo (replace with your own)".
- **The surveys.** Open each one in Survey123 and send a test response before
  the field teams use it.
- **Survey wording.** A few survey questions and field labels still read
  "DOT&PF", "AKDOT" or "Typhoon Halong". Edit them in Survey123 to suit you.

## Check a deployment again, or remove it

```
propy deployer\deploy.py --portal https://www.arcgis.com --check deployer\runs\<run>.json
propy deployer\deploy.py --portal https://www.arcgis.com --remove deployer\runs\<run>.json --yes
```

`--remove` deletes only what that run created (the items go to your recycle
bin), releases the Hub site's address, deletes the site's group, and deletes
the folder if nothing else is in it. It refuses to run in any other
organization or account than the one that made the run. Keep the run files:
they are the record of what to remove.

## What the kit leaves out

- The communities the last storm hit, and the map layers built for that event.
- The last event's drone imagery.
- Layers that belong to partner organizations.
- Every record, username, organization id and sign-in token.

Three public reference layers are still linked, not copied, in the two
Community Status maps: Alaska DOT&PF's route data and highway lighting, and
Alaska DEC's solid waste sites. Outside Alaska, remove them from those maps.

The kit cannot be deployed back into the Alaska DOT&PF organization it was
built from.

## Known gaps

- The Home Damage dashboard's count, chart and list read the Combined House
  Damage and Inspection layer, which a Data Pipeline filled in the original.
  The layer comes along empty; the pipeline does not.
- The Home Damage dashboard has no community selector. The original read a
  statewide communities layer in another organization.
- The request form is an Experience Builder app with an Edit panel, not a
  Survey123 form.
- The AI damage description notebook, the FEMA report notebook and the survey
  report templates are not in the kit.
- Sign-in is ArcGIS Pro's. There is no hosted version with its own sign-in.

## What is in this folder

| Path | What it is |
| --- | --- |
| `deploy.cmd` | Opens the deployer page |
| `deployer\` | The page (`server.py`, `index.html`), the deployment itself (`deploy.py`), the tools it builds new (`generated.py`, `templates\`) and the ArcGIS sign-in and requests (`portal.py`) |
| `kit\` | The kit: `kit.json` lists the tools and their items, and `items\` holds one template per item |
| `video-player\` | The drone video player page and an example of its config file |
| `tests\` | Tests that deploy every tool against a stand-in for ArcGIS Online. No network and no sign-in needed |

In a template, everything that belonged to the original organization is a
placeholder the deployer fills: `{{<item id>.itemId}}` and `{{<item id>.url}}`
for the new copies, `{{portal.url}}`, `{{user.username}}`, `{{event.name}}`
and `{{org.name}}`.

To run the tests:

```
propy -m pytest tests
```
