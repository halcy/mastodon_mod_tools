# Detect-and-Ban-Zone
A maybe future set of tools to help with mastodon moderation.

So far, there is a settings and log web app, an automated report / anti-spam 
tool (Goku - the Guarding Online Kommunications Utility) and a caching instance
databse (Piccolo - the Platform for Instance Cataloging (with Cache Of Last 
Operations)), thus far with no GUI.

Very very alpha software. Run at own risk. Known limitation currently: CLIP model
used isn't really good at non-latin charsets for text.

Planned:
* Bulma - the Broad Utility for Logging Moderation Activity, a tool for logging, 
  archiving and autoclosing (if remote already banned the user) reports
* Possibly tooling to un-limit users that local users follow even if they're on 
  limited instances
* Possibly tooling to also list media-silence instances because mastodon doesn't
  anymore (?) and to automatically revise suspend lists (prune dead instances,
  set auto-expiry for temp "oh shit they're getting overran by spam" type silences)
* Some tool to automatically update detection patterns based on what mods do with
  reports
* Maybe an API to get input from trusted other servers?
* Your idea here. Please tell me things you think would be useful to have.
