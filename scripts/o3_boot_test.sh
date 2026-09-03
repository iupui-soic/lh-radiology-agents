#!/usr/bin/env bash
# Boot-test an o3 image against a COPY of the demo database, without touching the live stack
# (run-book 6a). Added 2026-09-03 after o3-074c9b8d deployed as a bare core: a green docker-build
# proves the image assembles, not that OpenMRS starts its modules.
#   usage: scripts/o3_boot_test.sh <image-ref> <pre-deploy dump .sql.gz>
# Runs on the demo host beside the live stack: reads the live openmrs container's OMRS_* env and
# the live mariadb image, never its volume. Leaves o3test + o3test-db running for further checks;
# remove them with: docker rm -f o3test o3test-db; docker network rm o3test-net
set -u
IMG="${1:?image ref}"
DUMP="${2:?pre-deploy dump .sql.gz}"
NET=o3test-net
docker rm -f o3test o3test-db >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1
docker network create $NET >/dev/null
# env from the LIVE openmrs container (OMRS_* only), db host rewritten; values never printed
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' lh-radiology-agents-openmrs-1 | grep '^OMRS_' | sed 's/^OMRS_DB_HOSTNAME=.*/OMRS_DB_HOSTNAME=o3test-db/' > /tmp/o3test.env
chmod 600 /tmp/o3test.env
DBPW=$(grep '^OMRS_DB_PASSWORD=' /tmp/o3test.env | cut -d= -f2-)
echo "=== scratch db ($(date +%T)) ==="
MDB=$(docker inspect -f "{{.Image}}" lh-radiology-agents-mariadb-1)   # the live, digest-pinned mariadb image, no pull
docker run -d --name o3test-db --network $NET -e MARIADB_ROOT_PASSWORD=openmrs -e MARIADB_DATABASE=openmrs -e MARIADB_USER=openmrs -e MARIADB_PASSWORD="$DBPW" "$MDB" >/dev/null
for i in $(seq 1 30); do docker exec o3test-db mariadb -uroot -popenmrs -e "SELECT 1" >/dev/null 2>&1 && break; sleep 2; done
T=$(date +%s); zcat $DUMP | docker exec -i o3test-db mariadb -uroot -popenmrs openmrs && echo "dump loaded in $(( $(date +%s) - T ))s"
docker exec o3test-db mariadb -uroot -popenmrs openmrs -N -e "SELECT CONCAT('patients=',COUNT(*)) FROM patient; SELECT CONCAT('reports=',COUNT(*)) FROM radiology_report; DELETE FROM global_property WHERE property='search.indexVersion'; SELECT CONCAT('lucene GP rows=',COUNT(*)) FROM global_property WHERE property='search.indexVersion';" 2>/dev/null | tr '\n' ' '; echo
echo "=== boot $IMG ($(date +%T)) ==="
docker pull -q "$IMG" >/dev/null 2>&1 || true
docker run -d --name o3test --network $NET --env-file /tmp/o3test.env "$IMG" >/dev/null
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' o3test); echo "o3test ip $IP"
T0=$(date +%s)
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://$IP:8080/openmrs/ws/rest/v1/session || true)
  el=$(( $(date +%s) - T0 ))
  if [ "$code" = "200" ]; then echo "session 200 after ${el}s"; break; fi
  if docker logs o3test 2>&1 | grep -q "cancelling refresh"; then echo "ABORTED after ${el}s: context refresh cancelled"; break; fi
  [ $((i % 6)) -eq 0 ] && echo "  ${el}s: session -> $code"
  sleep 10
done
L=$(docker logs o3test 2>&1)
echo "=== markers: cancelling refresh=$(echo "$L" | grep -c 'cancelling refresh') Context.shutdown=$(echo "$L" | grep -c 'Context.shutdown') module start errors=$(echo "$L" | grep -c 'Error while trying to start module') ($(echo "$L" | grep -o 'Error while trying to start module: [a-z]*' | sort -u | tr '\n' ' '))"
echo "=== core: $(echo "$L" | grep -o 'openmrs-api-[0-9.A-Za-z-]*\.jar' | head -1)"
echo "=== handlers ==="
for p in "ws/rest/v1/session" "ws/fhir2/R4/metadata" "module/radiology/radiologyReport.form?orderId=x" "moduleResources/radiology/vendor/jquery/jquery.min.js" "moduleResources/radiology/vendor/tinymce/tinymce.min.js" "moduleResources/radiology/vendor/datatables/media/js/jquery.dataTables.min.js"; do printf '%-70s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code} %{size_download}B' --max-time 20 "http://$IP:8080/openmrs/$p")"; done
echo "=== lucene GP after boot: $(docker exec o3test-db mariadb -uroot -popenmrs openmrs -N -e "SELECT property_value FROM global_property WHERE property='search.indexVersion'" 2>/dev/null)"
echo "=== done ($(date +%T)); o3test + o3test-db left running for further checks ==="
