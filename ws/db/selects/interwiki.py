#!/usr/bin/env python3

def get_interwikimap(db):
    interwikimap = {}

    with db.engine.connect() as conn:
        for row in conn.execute(db.interwiki.select()):
            iw = {
                "prefix": row.iw_prefix,
                "url": row.iw_url,
            }
            if row.iw_local:
                iw["local"] = ""
            if row.iw_trans:
                iw["trans"] = ""
            if row.iw_api:
                iw["api"] = row.iw_api
            interwikimap[row.iw_prefix] = iw

    return interwikimap
