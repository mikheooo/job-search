# WHUBN

**Where Have You Been? — Your life, mapped.**

WHUBN is a local-first iOS app that reconstructs where a person's life happened from permitted location evidence such as photo metadata and location-history imports.

## Repository status

Stage 0 & 1 foundation:

- SwiftUI application shell
- platform-independent `WHUBNCore` Swift package
- unit-test target
- XcodeGen project specification
- macOS CI for project generation, build, and tests
- initial architecture, data-model, privacy, and decision records
- Stage 1 domain models and JSON round-trip tests

Stage 2 persistence:

- High-throughput SQLite persistence engine with WAL and Sendable Swift 6 concurrency
- Versioned database schema migrations (`MigrationV1InitialSchema`)
- Repository interfaces and database transaction abstraction (`DatabaseClient`, `DatabaseContext`)
- Full persistence coverage for all 9 domain models, relations, cascades, and batch operations
- Comprehensive test suite (CRUD, foreign key cascades, migrations, transactions/rollback, batch inserts)

Stage 3 scanning:

- Apple PhotoKit metadata scanner (`PhotoLibraryScanner`) and authorization handler (`PhotoLibraryAuthorizing`)
- Metadata extraction without explicitly requesting image/video media bytes (WHUBN does not explicitly request image/video media bytes during metadata scanning)
- Incremental scanning with persistent checkpoints (`ScanCheckpoint`) and batch tracking (`ImportBatch`)
- Idempotent deduplication using deterministic RFC 4122 identifiers
- Asynchronous progress streaming and safe cancellation

Stage 4 scan UI:

- Native SwiftUI scan flow (`ScanView`, `ScanProgressView`, `ScanAuthorizationView`, `ScanCompletionView`)
- Explicit state machine (`ScanState`) and `@MainActor` ViewModel (`ScanViewModel`)
- Real progress display (processed/total, percentage, GPS count, determinate progress bar)
- Cooperative cancellation UI and safe recovery
- Authoritative state restoration on relaunch from persisted database state

Stage 5 geographic resolution:

- Platform-independent reverse geocoding abstraction (`ReverseGeocodingProtocol`, `GeographicPlacemark`)
- Apple `CLGeocoder` adapter isolated in `ios/Infrastructure/Geocoding`
- Deterministic exact coordinate cache identity (`ExactCoordinateKey` at $10^{-6}$ degree precision) ensuring 100% boundary safety
- Bounded concurrency for reverse-geocoding requests with explicit retry semantics
- Local-first persistent geocoding cache (`geocoding_cache` table via `MigrationV2GeographicResolution` and `MigrationV3ExactCoordinateCache`)
- Place normalization and deterministic UUID generation for canonical countries, regions, and cities
- Distinct deduplicated queries for visited countries and cities (`GeographyQueryService`)

Stage 6 map MVP:

- Interactive 2D life map using Apple MapKit (`AtlasMapView`, `AtlasMKMapView`)
- Native MapKit clustering (`MKMarkerAnnotationView` with `clusteringIdentifier = "WHUBNPlaceCluster"`) displaying real place counts
- Lightweight place selection overlay (`PlaceDetailCard`) showing authoritative city, country, region, and evidence count without premature Stay/Trip claims
- Intentional empty and resolution-pending states (`AtlasEmptyStateView`)
- Platform-independent map presentation model and bounds computation (`MapPresentationService`, `MapPlace`, `MapCoordinateBounds`)
- Zero live GPS tracking / no background location permissions

Stage 7 stay engine v1:

- Deterministic chronological segmentation of `LocationEvent` records into canonical `Stay` records (`StaySegmentationEngine`)
- Configurable maximum evidence gap (`StayDetectionOptions.maximumEvidenceGap`, default 24 hours) preventing sparse photos from implying continuous presence
- Place changes and returns to the same Place correctly produce distinct `Stay` records
- Idempotent `LocationEventBridge` preserving raw evidence provenance
- Atomic Stay rebuilds with transaction rollback and cooperative cancellation safety (`StayEngineService`)
- Linear $\mathcal{O}(N \log N)$ execution validated with 50,000 synthetic events

Stage 8 residence engine v1:

- Deterministic inference of `ResidencePeriod` records based on distinct calendar day support in temporal analysis windows (`ResidenceSegmentationEngine`)
- Multi-place place-day denominator ($\text{dominance}(P) = \text{placeDays}[P] / \sum \text{placeDays}$)
- Finite evidence-derived bounds for automatically inferred periods; open-ended bounds reserved for user-confirmed residences
- Independent Stay-evidence requalification and boundary derivation for trimmed/split fragments
- Explicit user-confirmed conflict detection (`ResidenceTimelineValidation`)
- Rebuild preserves user-confirmed residences (`confirmedByUser == true`)
- Linear $\mathcal{O}(N \log N)$ execution validated with 10,000 synthetic stays

Stage 9 trip engine v1:

- Evidence-based inference of `Trip` records from `Stay` and authoritative `ResidencePeriod` timelines (`TripSegmentationEngine`)
- Requires active residence baseline; suppresses automatic inference across unknown or conflicted residence periods
- Groups contiguous multi-city and multi-country away periods under configurable evidence gap thresholds (`TripDetectionOptions`)
- Evidence-derived boundaries (`startDate` and `endDate` equal earliest and latest away evidence bounds)
- Distinguishes genuine residence relocations from trips
- Atomic database replacement, full idempotency, and cancellation safety (`TripEngineService`)
- Linear $\mathcal{O}(N \log N)$ execution validated with 10,000 synthetic stays

Stage 10 trips UI:

- Native SwiftUI Trips browsing feature (`TripsView`, `TripRowView`, `TripDetailView`) integrated directly into the primary application `TabView`
- Deterministic chronological descending sorting (newest first) and year grouping by trip start year
- Platform-independent presentation service (`TripsPresentationService`) and view models (`TripsViewModel`, `TripsState`)
- Single bulk Place query architecture (`PlaceRepository.fetch(byIDs:)`) completely eliminating N+1 database queries
- Clear evidence-honest date formatting (`TripDatePresentation`), displaying observed evidence bounds without fabricating departure or arrival timestamps
- Multi-city and multi-country deterministic presentation; disambiguation for same-name destinations (*Paris, France* vs *Paris, Texas, USA*) using canonical `Place.id` and `trip_destinations`
- Clean empty state with evidence-oriented copy and error states with recovery options
- 100% test coverage for presentation logic, date edge cases, sorting, and large 5,000-trip benchmarks

Stage 11 life timeline:

- Unified chronological Life Timeline combining persisted `ResidencePeriod` and `Trip` records (`ResidencePeriod + Trip -> TimelinePresentationService -> TimelineViewModel -> TimelineView`)
- Native SwiftUI Timeline feature (`TimelineView`, `TimelineRowView`, `ResidenceTimelineRowView`, `ResidenceDetailSheet`) integrated into the primary application `TabView`
- Deterministic descending chronological sorting and year grouping by `startDate` year (multi-year residences appear once under start year)
- Natural preservation of normal Residence and Trip overlaps (trips during a residence period appear without trimming or partitioning)
- Explicit surfacing of user-confirmed residence conflicts (`hasConfirmedConflicts: true`, accessible warning banner) without destructive automatic repair
- Evidence-honest date bounds for automatic residences; open-ended "Present" supported strictly for user-confirmed current residences
- Timeline creates no new inference and does not fabricate unknown chronological gaps
- Trip dates remain evidence-derived away bounds; trip distance is not presented as actual traveled distance
- Chunked bulk Place loading ($2 + \lceil U / 500 \rceil$ queries), fully eliminating per-item N+1 queries
- Linear $\mathcal{O}(N \log N)$ execution validated with 10,000 synthetic timeline records (residences + trips)



## Requirements

- macOS with Xcode 16 or newer
- XcodeGen (`brew install xcodegen`)

## Open and run

```sh
xcodegen generate
open WHUBN.xcodeproj
```

To run the core tests without opening Xcode:

```sh
swift test --package-path core
```

## Principles

- Original photos stay on-device unless a future feature explicitly requires upload and the user permits it.
- Raw evidence stays separate from derived results.
- Inferences carry confidence; user corrections take precedence.
- Source integrations are isolated behind adapters.

See [docs/PROJECT.md](docs/PROJECT.md) for scope and milestones.
