# OpenHIS reader release

The [reader release](https://github.com/Ricardo-Wang123/TwinHospital-Reproducibility/releases/tag/openhis-reader-v1) provides the OpenHIS backend used by TwinHospital and a database initialized for new cases.

| File | Contents |
| --- | --- |
| `openhis-twinhospital-runtime.tar.gz` | Java 17 OpenHIS 2.0.5 JAR, PostgreSQL seed, and local role provisioning files. Extracts to `openhis-reader/`. |
| `openhis-backend-source-2.0.5.tar.gz` | Corresponding OpenHIS Java and Maven source with the upstream build guide, LICENSE, and NOTICE. |
| `twinhospital-fictional-cases.tar.gz` | Five editable example cases and a portable manifest. |
| `SHA256SUMS` | SHA-256 of each archive. |

Docker Compose runs OpenHIS, PostgreSQL, and a loopback gateway. The Python MAS invokes the application's registration, diagnosis, order, document, and settlement APIs. OpenHIS records simulated cash settlement in the local database. The MAS stores the role views, dialogue, and artifact lineage alongside each encounter. [Compose configuration](compose.yaml) · [OpenHIS adapter](../../sandbox/experiments/linked/native_his.py)

If the default TwinHospital deployment is already running, use a separate checkout and set an unused project name and port before configuring, starting, or auditing the new run.

~~~sh
export TH_OPENHIS_PROJECT=twinhospital-exp-smoke-reader
export TH_OPENHIS_PORT=28082
~~~

The startup database uses the matching OpenHIS schema, software terminology, frequencies, access roles, and settings. The virtual institution, registration service, self-pay contract, document type, and pharmacy routing are defined in [reader-bootstrap.sql](reader-bootstrap.sql). Provisioning adds local role accounts, a rolling schedule, and the simulated formulary. Patients, encounters, prescriptions, notes, and transactions start empty. [Schedule](reader-schedule.sql) · [Formulary fixture](experiment-fixtures.sql)

For a source build, use JDK 17 and Maven from the extracted backend source directory. The resulting JAR is `whale-health/health-app/target/health-app-2.0.5.jar`.

~~~sh
mvn -B -ntp -pl whale-health/health-app -am -DskipTests package
~~~

The research cohort used the original matching release identified by the frozen input checksums. The reader release uses the same backend with an independent startup database. [Study reproduction](../../handoff/REPRODUCE.md#repeat-the-study-configuration)
