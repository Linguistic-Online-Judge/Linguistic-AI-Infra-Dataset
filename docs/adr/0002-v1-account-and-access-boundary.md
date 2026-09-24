# Architecture Decision Record 0002: V1 Account and Access Boundary

Status: Accepted

## Context

The project needs an account model before the student web interface is finalized.
The supervising teacher confirmed that the public service does not need the
school eHall identity system. The service is expected to be deployed on an
overseas server and made publicly accessible at a later stage. The application
needs only administrator accounts and general user accounts; it does not need
separate student and teacher roles.

## Decision

- Authentication remains a server-verified, replaceable integration. It is not
  coupled to the school eHall system.
- V1 has exactly two application roles: `user` and `admin`.
- New accounts default to `user`. Creating an `admin` remains a trusted
  server-side operation and is not exposed through the public API.
- General users retain owner-scoped access to their submissions and results.
- This decision records the role boundary only. It does not invent a registration
  method, password policy, email requirement, or administrator feature set.
- The team owns the visual design. The East China Normal University online judge
  may be used as a reference, but it is not a required visual specification.

## Consequences

- User records and the current-user API response include the application role.
- Existing users are migrated to `user`; no existing account is silently promoted.
- Future administrator endpoints must check `admin` explicitly and receive their
  own review and tests.
- Public deployment still requires production authentication, HTTPS, abuse
  protection, rights approval, monitoring, and backup recovery evidence.

## Unresolved Product Decisions

- Whether people register themselves or administrators create accounts.
- Whether registration uses a username, email address, or another identifier.
- Whether account recovery and email verification are required for V1.
- Which management actions administrators are allowed to perform.
- Whether anonymous visitors may browse challenges and leaderboards.
