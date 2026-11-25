widefrog fork
---------------
Fork of https://github.com/mycodedoesnotcompile2/widefrog where I apply some patches.  
Feel free to contribute.

Videoland catalog indexer
-------------------------
Index public metadata from a Videoland page through Widefrog:

```sh
python widefrog.py --index "https://v2.videoland.com/schiphol-diamantroof-p_5841"
```

The indexer reports page metadata, episode-card titles and runtimes, and same-site
links. It does not retrieve video streams or process DRM/license data. Episode
cards that link to Videoland's subscription page are reported as requiring a
subscription.
