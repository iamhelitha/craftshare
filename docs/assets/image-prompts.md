# README artwork

Generated using the built-in image_gen tool. Final prompts are recorded below for future revisions.

## craftshare-banner.png

Create a polished wide GitHub README hero banner, landscape 3:1, for the open source app CraftShare. Use case ads-marketing. Exact large title "CraftShare". Exact subtitle "Your PC. Your friends. One shared world." Small footer text "Minecraft Java server manager". Premium voxel/isometric illustration, dark midnight navy background, glowing mint green and cyan accents, finely detailed floating grass-and-stone island with a blocky tree and small house, a desktop computer on another small island, and a tiny glowing server tower connected by elegant luminous paths. Beautiful restrained depth, crisp shapes, atmospheric lighting, professional open source project branding, generous margins. Title on left with excellent readability and illustration concentrated on right. No official Minecraft logo, no watermarks, no other text. Save generated output and return its local file path.

## craftshare-how-it-works.png

Create a highly legible polished technical infographic banner for CraftShare GitHub README, wide landscape 2:1 aspect ratio. Dark midnight navy, mint green and cyan accents, matching premium voxel/isometric computer and server illustrations with clean flat diagram connectors. Headline "How CraftShare works". Two horizontal panels with clear whitespace and large readable typography.
TOP PANEL title "HOST & SHARE". Left large card "Host PC" containing "CraftShare dashboard" and "Minecraft server :25565". Center-right card "VPS" containing "Public address :25565", "Shared world + host lock", "mc-hub: invites & access". Far right small card "Friends" with computer icons. Friends connect to VPS public address, and VPS forwards game traffic to Host PC through SSH reverse tunnel. Draw cyan bidirectional connector Host PC <-> VPS labeled "SSH reverse tunnel", and connector Friends <-> VPS labeled "Play". A separate mint bidirectional line below Host PC and VPS labeled "SFTP world sync" with short captions "Download before hosting" and "Upload after stopping". Under VPS small text "Minecraft runs on the host PC". Do not imply Minecraft runs on VPS.
BOTTOM PANEL title "OPTIONAL PROXY ON YOUR PC". Two very clean left-to-right flow rows with arrowheads:
Row 1: "SSH / SFTP" -> "SOCKS5 proxy" -> "VPS SSH :22".
Row 2: "Minecraft client" -> "Join helper :25566" -> "SOCKS5 proxy" -> "VPS :25565".
Put the proxy example text "e.g. v2rayN • 127.0.0.1:10808" below the SOCKS5 nodes. In row 2 the join helper and proxy run on the player's PC, useful when joining someone else's host. Footer exact text "Blank proxy = direct connection • One host at a time • No router port forwarding".
Use simple unmistakable arrows, no crossing lines, exact spelling, avoid extra labels or paragraphs. Functional explanatory diagram with tasteful voxel icons, generous spacing, restrained glow. No watermarks.
