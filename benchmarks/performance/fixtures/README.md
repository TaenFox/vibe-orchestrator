# Fixtures

Генератор использует только seed-зависимые synthetic identifiers `FIX-*`/`SESSION-*`/`RUN-*`, фиксированные timestamps и пустые текстовые поля, требуемые production-схемой. Непустые titles, descriptions, prompts, raw payloads и production identifiers запрещены. Размеры: small=100, medium=1000, large=5000, xlarge=10000 tickets; фактические counts и распределения состояний записываются в manifest. Одинаковые seed/profile/storage дают одинаковый logical checksum, разные seed дают разные данные. SQLite и legacy YAML материализуются через соответствующие store adapters.
