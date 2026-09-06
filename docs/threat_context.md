# Threat context

This document exists so that the problem statement can be defended from
sources rather than asserted. Every claim below is attributed, and the section
at the end states plainly what these sources do **not** establish.

## Intentional GNSS interference is present in Cypriot airspace

EASA maintains a list of Flight Information Regions affected by GNSS jamming
and spoofing, updated weekly. **`LCCC NICOSIA` — the FIR covering Cyprus —
appears in both the 7-day and 30-day tables, flagged for jamming and spoofing**
(consulted 6 September 2026).

> "Since February 2022, there has been a notable increase in GNSS jamming and
> spoofing, particularly in regions surrounding conflict zones and other
> sensitive areas such as the Mediterranean, Black Sea, Middle East, Baltic Sea,
> and the Arctic."
>
> — [EASA, *GNSS Outages and Alterations*](https://www.easa.europa.eu/en/domains/air-operations/global-navigation-satellite-system-outages-and-alterations)

The standing safety bulletin is
[EASA SIB 2022-02R4](https://www.easa.europa.eu/en/newsroom-and-events/news/easa-updates-safety-information-bulletin-gnss-interference),
*Global Navigation Satellite System (GNSS) Outage and Alterations Leading to
Navigation / Surveillance Degradation*, issued 3 July 2026. Airspace
information was deliberately moved out of the bulletin and onto the webpage
above, because it changes faster than a bulletin can be revised.

## It has been measured in flight, in this FIR

The strongest citation is not a bulletin but a peer-reviewed flight experiment
conducted in the Nicosia FIR itself:

> Osechas, O., Fohlmeister, F., Dautermann, T., & Felux, M. (2022).
> *Impact of GNSS-Band Radio Interference on Operational Avionics.*
> NAVIGATION: Journal of the Institute of Navigation, 69(2).
> [doi:10.33012/navi.516](https://doi.org/10.33012/navi.516)

On 13 February 2020, flying instrumented commercial avionics in the Nicosia
FIR, the authors recorded:

| Observation | Value |
| --- | --- |
| Interference signature | Triangle-wave frequency chirp, ~1 ms period |
| Received power | At least 30 dB above the noise floor at the aircraft |
| Bands affected | L1 (1575.42 MHz) and L5 (1176.45 MHz) |
| Multimode receiver positioning lost | **80% of experiment time** |
| Satellites tracked | Fewer than four for most airborne segments |
| Fallback | Inertial navigation |

The chirp signature is characteristic of a deliberate jammer rather than
incidental interference, and the received power implies a high-power source.
Performance-based navigation was not achievable during the outage.

## Why this project is a response to it

The operational consequence is the part worth stating carefully. Interference
of this kind does not politely announce itself, and it does not stop an
aircraft flying. It removes the shared picture that coordination depends on,
for an interval nobody can predict, while every aircraft involved is still
airborne and still tasked.

That is precisely the condition this simulator is built to reason about: not
whether a vehicle survives, but whether the *rest of the formation* draws the
right conclusion about it. A swarm that requires agreement in order to
reallocate work is a swarm that stops working at exactly the moment the
environment stops delivering agreement.

## What these sources do not establish

Stated explicitly, because conflating these would be the easiest way to lose
credibility with anyone who knows RF.

- **GNSS denial is not comms jamming.** The sources above concern interference
  with *satellite navigation* signals. This project models degradation and
  partitioning of the *inter-vehicle communication* link. Both are consequences
  of a contested RF environment, and they frequently co-occur, but they are
  different mechanisms and nothing here should be read as evidence about the
  other.
- **They do not calibrate the radio model.** The simulator's propagation model
  remains an analytical free-space abstraction that has never been compared
  against measurement. The 30 dB figure above describes a real jammer against
  aviation receivers; it is not a parameter of this simulation and must not be
  presented as one. See *What is not measured* in
  [`measured_results.md`](measured_results.md).
- **They say nothing about UAVs specifically.** The measurements are from
  crewed aviation avionics. The inference that small uncrewed systems are at
  least as exposed is reasonable, and it is an inference, not a citation.

## Sources

- [EASA — GNSS Outages and Alterations](https://www.easa.europa.eu/en/domains/air-operations/global-navigation-satellite-system-outages-and-alterations) (affected-FIR tables, updated weekly)
- [EASA SIB 2022-02R4](https://www.easa.europa.eu/en/newsroom-and-events/news/easa-updates-safety-information-bulletin-gnss-interference) (3 July 2026)
- [Osechas et al. (2022), NAVIGATION 69(2), doi:10.33012/navi.516](https://doi.org/10.33012/navi.516)
