"""Short spatial continuity, never a substitute for fresh person observations."""
import time


def retention_threshold(threshold):
    return threshold * .7


def overlap(a, b):
    intersection = max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union else 0.


class PersonContinuity:
    """Weak observations may continue a recent strong match, but cannot start one.

    No predicted/held boxes are returned. Weak-only continuation lasts at most
    three seconds; tracks expire after 1.5 seconds without a matching observation.
    IDs are local to one camera connection, not identities of people.
    """
    def __init__(self, confirmations=1):
        self.tracks = {}
        self.next_id = 1
        self.confirmations = confirmations

    def update(self, detections, threshold, at=None):
        at = time.monotonic() if at is None else at
        self.tracks = {key: value for key, value in self.tracks.items() if at-value['seen'] <= 1.5}
        detections = [d for d in detections if d['score'] >= retention_threshold(threshold)][:100]
        matches = sorted(((overlap(track['box'], d['box']), key, index)
                          for key, track in self.tracks.items() for index, d in enumerate(detections)), reverse=True)
        assigned, used = {}, set()
        for score, key, index in matches:
            if score < .25 or key in used or index in assigned:
                continue
            if detections[index]['score'] < threshold and at-self.tracks[key]['strong'] > 3:
                continue
            assigned[index] = key
            used.add(key)
        for key, track in self.tracks.items():
            if key not in used and not track.get('confirmed', False):
                track['hits'] = 0
        result = []
        for index, detection in enumerate(detections):
            key = assigned.get(index)
            strong = detection['score'] >= threshold
            if key is None:
                if not strong:
                    continue
                key = self.next_id
                self.next_id += 1
            old = self.tracks.get(key, {})
            hits = old.get('hits', 0)+1 if strong else 0
            confirmed = old.get('confirmed', False) or hits >= self.confirmations
            self.tracks[key] = {'box': list(detection['box']), 'seen': at, 'strong': at if strong else old['strong'], 'hits': hits, 'confirmed': confirmed}
            if confirmed:
                result.append({**detection, 'track_id': key})
        return result
